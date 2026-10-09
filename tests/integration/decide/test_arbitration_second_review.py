"""`POST /decide/arbitrate` after the second review (Plan J M101, DEC-1311).

The runs are the product's own (`tests/fixtures/decide/treat_runs.py`), their treat lists built by
`build_treat_list`, and every check goes through the HTTP routes:

* each use case's campaign compares like with like (the treated and the held-back arm are cut by one rule);
* the campaign has the terms `POST /campaigns` gives: the run's finish time, the use case's outcome window,
  the hold-out requirement; posting twice makes one set of campaigns;
* no customer-level file is kept outside the runs and campaigns, and retention finds the arbitrated copies;
* the latest run of a use case is the one that finished last;
* a settings file that cannot be read is a 422 with its code, and a run without a treat list says why.
"""

from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.access.roles import Role
from engine.contracts import RunRecord, RunState
from engine.decide.arbitrate import (
    ARBITRATED_TREAT_LIST_CSV,
    ARBITRATED_TREAT_LIST_PARQUET,
    ARBITRATION_SUMMARY_FILENAME,
)
from engine.decide.treat_list import ensure_treat_list, policy_intended
from engine.measurement.campaign import (
    ASSIGNMENT_FILENAME,
    InMemoryCampaignStore,
    campaign_key,
    read_frame,
)
from engine.privacy.retention import plan_retention
from engine.runs import RUN_FILENAME
from engine.storage import LocalStorage, run_key
from tests.fixtures.decide.arbitration_runs import run_for_use_case
from tests.fixtures.decide.treat_runs import FINISHED, write_run
from tests.integration.production.access_support import bearer, local_app, make_user

pytestmark = pytest.mark.integration


def _prepare(
    tmp_path: Path, runs: list[tuple[str, dict[str, Any]]]
) -> tuple[FastAPI, TestClient, InMemoryCampaignStore, list[str]]:
    storage = LocalStorage(tmp_path)
    run_ids = []
    for use_case, options in runs:
        run_id = run_for_use_case(storage, use_case, **options)
        ensure_treat_list(storage, run_id)
        run_ids.append(run_id)
    store = InMemoryCampaignStore()
    app = local_app(tmp_path)
    app.state.campaign_store = store
    return app, TestClient(app, raise_server_exceptions=False), store, run_ids


def _user(app: FastAPI, name: str, role: Role) -> dict[str, str]:
    """Headers for a signed-in user of this role, made once per app."""
    users: dict[str, str] = app.state.__dict__.setdefault("_test_users", {})
    if name not in users:
        users[name] = make_user(app, name, [role])
    return bearer(app, users[name])


def _post(app: FastAPI, client: TestClient, body: dict[str, Any]) -> Any:
    return client.post("/decide/arbitrate", json=body, headers=_user(app, "arbitrator", Role.ANALYST))


def _shares(frame: pd.DataFrame, column: str) -> pd.Series[float]:
    return frame[column].astype(str).value_counts(normalize=True)


def _standardised_difference(left: pd.Series[float], right: pd.Series[float]) -> float:
    pooled = float(pd.concat([left, right]).std())
    return float(left.mean() - right.mean()) / pooled if pooled > 0 else 0.0


def _max_gap(left: pd.DataFrame, right: pd.DataFrame, column: str) -> float:
    a, b = _shares(left, column), _shares(right, column)
    return float(a.sub(b, fill_value=0.0).abs().max())


# ---------------------------------------------------------------------------
# Blocker: the two arms are comparable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "rows"), [("propensity", (2000, 2000)), ("uplift", (2400, 1200))], ids=["propensity", "uplift"]
)
def test_the_treated_and_the_held_back_arm_have_the_same_make_up(
    tmp_path: Path, kind: str, rows: tuple[int, int]
) -> None:
    """Fails on cb43c05: the treated arm shrank to the winners while the held-back arm kept every eligible
    customer, so the arms differed in band (propensity) or lost customers to a rival on one side only
    (uplift), and the measured effect came from who was in each arm."""
    options: dict[str, Any] = {"kind": kind, "value": True, "holdout": True}
    app, client, store, run_ids = _prepare(
        tmp_path,
        [
            ("uc-a", options | {"rows": rows[0]}),
            ("uc-b", options | {"rows": rows[1], "config_overrides": {"uplift.policy.margin_pct": 45.0}}),
        ],
    )
    response = _post(app, client, {"use_cases": ["uc-a", "uc-b"]})
    assert response.status_code == 200, response.text
    storage = LocalStorage(tmp_path)
    assert len(response.json()["campaign_ids"]) == 2
    for campaign_id in response.json()["campaign_ids"]:
        campaign = store.get(campaign_id)
        assert campaign is not None
        assignment = read_frame(storage, campaign_key(campaign.campaign_id, ASSIGNMENT_FILENAME))
        within = assignment[assignment["intended"]]
        treated, held = within[within["arm"] == "treated"], within[within["arm"] == "holdout"]
        assert len(treated) > 60 and len(held) > 30, (campaign.use_case_id, len(treated), len(held))
        # The band (the make-up of a propensity list) and the value each customer was ranked on.
        gap = _max_gap(treated, held, "band")
        assert gap < 0.15, f"{campaign.use_case_id}: band shares differ by {gap:.2f} between the arms"
        # The share of each arm that a rival use case also wanted: equal, because a rival takes customers
        # from both arms by one rule (cb43c05 took them from the treated arm only).
        other_run = next(r for r in run_ids if r != campaign.run_ids[0])
        rival = policy_intended(storage, other_run)
        rival_wants = set(rival[rival].index)
        contested = [part["customer_id"].isin(rival_wants).mean() for part in (treated, held)]
        assert abs(contested[0] - contested[1]) < 0.15, (campaign.use_case_id, contested)
        scores = pd.read_parquet(
            io.BytesIO(storage.read_bytes(run_key(campaign.run_ids[0], "scores.parquet")))
        )
        value = "net_value" if "net_value" in scores.columns else "winback_prob"
        by_customer = scores.set_index("customer_id")[value]
        smd = _standardised_difference(
            treated["customer_id"].map(by_customer).astype(float),
            held["customer_id"].map(by_customer).astype(float),
        )
        assert (
            abs(smd) < 0.25
        ), f"{campaign.use_case_id}: the arms differ in {value} by {smd:.2f} standard deviations"


def test_a_customer_a_rival_would_have_won_is_in_neither_arm(tmp_path: Path) -> None:
    """Uplift: B's priority times net value takes contested customers from A; they leave A's treated arm AND
    A's held-back arm, and the ones A would win stay in both."""
    options: dict[str, Any] = {"kind": "uplift", "rows": 1200, "value": True, "holdout": True}
    app, client, store, run_ids = _prepare(
        tmp_path,
        [
            ("uc-a", options | {"config_overrides": {"uplift.policy.margin_pct": 60.0}}),
            ("uc-b", options | {"config_overrides": {"uplift.policy.margin_pct": 30.0}}),
        ],
    )
    root = tmp_path / "config-root"
    (root / "decide").mkdir(parents=True)
    (root / "decide" / "arbitration.yaml").write_text(
        "use_cases:\n  uc-b:\n    priority: 2.1\n", encoding="utf-8"
    )
    app.state.config_root = root
    body = _post(app, client, {"use_cases": ["uc-a", "uc-b"]}).json()
    storage = LocalStorage(tmp_path)
    campaign = next(
        c for c in map(store.get, body["campaign_ids"]) if c is not None and c.use_case_id == "uc-a"
    )
    assignment = read_frame(storage, campaign_key(campaign.campaign_id, ASSIGNMENT_FILENAME)).set_index(
        "customer_id"
    )
    lists = [
        pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(run_id, "treat_list.parquet"))))
        for run_id in run_ids
    ]
    flags = [policy_intended(storage, run_id) for run_id in run_ids]
    both = lists[0].merge(lists[1], on="customer_id", suffixes=("_a", "_b"))
    explored = both["explore_a"].astype(bool) | both["explore_b"].astype(bool)
    wanted = both[
        both["customer_id"].map(flags[0]).astype(bool)
        & both["customer_id"].map(flags[1]).astype(bool)
        & both["net_value_a"].notna()
        & both["net_value_b"].notna()
        & ~explored
    ]
    b_wins = wanted[2.1 * wanted["net_value_b"] > wanted["net_value_a"]]  # B's priority is 2.1
    a_wins = wanted[2.1 * wanted["net_value_b"] <= wanted["net_value_a"]]
    arm_of = assignment["arm"]

    for group, name in ((b_wins, "B wins"), (a_wins, "A wins")):
        for held, arm in ((True, "holdout"), (False, "treated")):
            members = group[group["holdout_a"] == held]["customer_id"]
            if held is False:
                members = group[group["treat_a"]]["customer_id"]
            assert len(members) >= 10, (name, arm, len(members))
            kept = arm_of.loc[members] == (arm if name == "A wins" else "suppressed")
            assert kept.all(), f"{name}: customers of A's {arm} arm are not where the rule puts them"
            assert (assignment.loc[members, "intended"] == (name == "A wins")).all()


# ---------------------------------------------------------------------------
# Major: the terms POST /campaigns gives
# ---------------------------------------------------------------------------


def test_the_campaign_starts_when_the_run_finished_and_has_the_use_cases_window(tmp_path: Path) -> None:
    """Fails on cb43c05: the start was the run's creation (labelled "run finished") and the window was empty."""
    app, client, store, _ = _prepare(
        tmp_path,
        [
            # churn-prevention scores more customers than win-back, so it wins some: a customer in win-back's
            # control group is no longer free for it to take (DEC-1311 (al)), and two lists of the very same
            # customers would leave it with none.
            ("win-back-campaign", {"kind": "propensity", "rows": 100}),
            ("churn-prevention", {"kind": "propensity", "rows": 120}),
        ],
    )
    response = _post(app, client, {"use_cases": ["win-back-campaign", "churn-prevention"]})
    assert response.status_code == 200, response.text
    storage = LocalStorage(tmp_path)
    for entry in response.json()["campaigns"]:
        assert entry["outcome"] == "created", entry
        campaign = store.get(entry["campaign_id"])
        record = storage.read_model(run_key(entry["run_id"], RUN_FILENAME), RunRecord)
        assert campaign is not None and record.finished_at is not None
        assert campaign.treatment_start == record.finished_at
        assert campaign.treatment_start > record.created_at
        assert campaign.treatment_start_source == "run_finished"
        assert campaign.outcome_window_days == 90, "the use case's own window, as POST /campaigns reads it"
        assert campaign.arbitration_id is not None and campaign.population == "bands"


def test_posting_twice_makes_one_set_of_campaigns(tmp_path: Path) -> None:
    """Fails on cb43c05: every POST made a new LIVE campaign per run over the same lists."""
    app, client, store, run_ids = _prepare(
        tmp_path,
        [
            ("uc-a", {"kind": "propensity", "rows": 100}),
            ("uc-b", {"kind": "uplift", "rows": 100, "value": True}),
        ],
    )
    first = _post(app, client, {"use_cases": ["uc-a", "uc-b"]}).json()
    second = _post(app, client, {"use_cases": ["uc-a", "uc-b"]}).json()
    assert [c["outcome"] for c in first["campaigns"]] == ["created", "created"]
    assert [c["outcome"] for c in second["campaigns"]] == ["reused", "reused"]
    assert second["campaign_ids"] == first["campaign_ids"]
    for run_id in run_ids:
        assert len(store.list(run_id=run_id)) == 1

    # Other settings are another arbitration: its campaigns are new.
    root = tmp_path / "config-root"
    (root / "decide").mkdir(parents=True)
    (root / "decide" / "arbitration.yaml").write_text(
        "use_cases:\n  uc-b:\n    priority: 2.0\n", encoding="utf-8"
    )
    app.state.config_root = root
    third = _post(app, client, {"use_cases": ["uc-a", "uc-b"]}).json()
    assert [c["outcome"] for c in third["campaigns"]] == ["created", "created"]
    assert set(third["campaign_ids"]).isdisjoint(first["campaign_ids"])


def test_a_use_case_with_no_winner_or_no_holdout_has_no_campaign_and_says_why(tmp_path: Path) -> None:
    """Fails on cb43c05: a LIVE campaign was made for a use case that won nobody or holds nobody back."""
    app, client, store, run_ids = _prepare(
        tmp_path,
        [
            ("uc-strong", {"kind": "propensity", "rows": 100}),
            ("uc-weak", {"kind": "propensity", "rows": 100}),
            (
                "uc-nohold",
                {
                    "kind": "propensity",
                    "rows": 100,
                    "config_overrides": {"actions.control_group_fraction": 0.0},
                },
            ),
        ],
    )
    # uc-weak's list treats nobody this cycle (every customer was suppressed, say): it has nothing to measure.
    storage = LocalStorage(tmp_path)
    weak_key = run_key(run_ids[1], "treat_list.parquet")
    weak = pd.read_parquet(io.BytesIO(storage.read_bytes(weak_key))).assign(treat=False)
    buffer = io.BytesIO()
    weak.to_parquet(buffer, index=False)
    storage.write_bytes(weak_key, buffer.getvalue())

    body = _post(app, client, {"use_cases": ["uc-strong", "uc-weak", "uc-nohold"]}).json()
    entries = {c["use_case_id"]: c for c in body["campaigns"]}
    assert entries["uc-strong"]["outcome"] == "created"
    assert entries["uc-nohold"]["outcome"] == "skipped"
    assert "holds nobody back" in entries["uc-nohold"]["reason"]
    assert entries["uc-weak"]["outcome"] == "skipped"
    assert "won no customer" in entries["uc-weak"]["reason"]
    assert body["campaign_ids"] == [entries["uc-strong"]["campaign_id"]]
    assert len(store.list(limit=100)) == 1


# ---------------------------------------------------------------------------
# Major: no customer-level copy outside the runs, and retention finds the ones in them
# ---------------------------------------------------------------------------


def test_no_customer_id_is_kept_outside_the_runs_and_campaigns(tmp_path: Path) -> None:
    """Fails on cb43c05, which wrote the whole arbitrated list to `decide/`, where retention never looked."""
    app, client, _, run_ids = _prepare(
        tmp_path, [("uc-a", {"kind": "propensity", "rows": 60}), ("uc-b", {"kind": "propensity", "rows": 60})]
    )
    assert _post(app, client, {"use_cases": ["uc-a", "uc-b"]}).status_code == 200
    storage = LocalStorage(tmp_path)
    outside = [k for k in storage.list_keys("") if not k.startswith(("runs/", "campaigns/", "uploads/"))]
    assert f"decide/{ARBITRATION_SUMMARY_FILENAME}" in outside
    for key in outside:
        assert b"C-0" not in storage.read_bytes(key), f"{key} holds a customer id"

    # Retention finds the arbitrated copies in the runs, and deletes them when the runs are due.
    plan = plan_retention(storage, None, FINISHED + timedelta(days=4000))
    planned = {item.key for item in plan.items}
    for run_id in run_ids:
        assert run_key(run_id, ARBITRATED_TREAT_LIST_CSV) in planned
        assert run_key(run_id, ARBITRATED_TREAT_LIST_PARQUET) in planned
        assert run_key(run_id, ARBITRATION_SUMMARY_FILENAME) not in planned, "the summary is an aggregate"


def test_the_downloads_come_from_a_run_that_still_keeps_the_list(tmp_path: Path) -> None:
    app, client, _, run_ids = _prepare(
        tmp_path, [("uc-a", {"kind": "propensity", "rows": 40}), ("uc-b", {"kind": "propensity", "rows": 40})]
    )
    assert _post(app, client, {"use_cases": ["uc-a", "uc-b"]}).status_code == 200
    storage = LocalStorage(tmp_path)
    headers = _user(app, "downloader", Role.ANALYST)

    first = client.get("/decide/arbitrated-treat-list.csv", headers=headers)
    assert first.status_code == 200
    assert first.content == storage.read_bytes(run_key(run_ids[0], ARBITRATED_TREAT_LIST_CSV))
    parquet = client.get("/decide/arbitrated-treat-list.parquet", headers=headers)
    assert len(pd.read_parquet(io.BytesIO(parquet.content))) == len(pd.read_csv(io.BytesIO(first.content)))

    # The first run's copy aged out: the second run's identical copy is served.
    storage.delete(run_key(run_ids[0], ARBITRATED_TREAT_LIST_CSV))
    assert client.get("/decide/arbitrated-treat-list.csv", headers=headers).content == first.content
    # Every copy gone: a plain 404, not a stale file.
    storage.delete(run_key(run_ids[1], ARBITRATED_TREAT_LIST_CSV))
    gone = client.get("/decide/arbitrated-treat-list.csv", headers=headers)
    assert gone.status_code == 404 and gone.json()["detail"]["code"] == "ARBITRATION_NOT_FOUND"


def test_the_summary_is_a_run_artefact_of_every_run_that_took_part(tmp_path: Path) -> None:
    """Fails on cb43c05: the runs route accepted the name but nothing wrote the file there."""
    app, client, _, run_ids = _prepare(
        tmp_path, [("uc-a", {"kind": "propensity", "rows": 40}), ("uc-b", {"kind": "propensity", "rows": 40})]
    )
    posted = _post(app, client, {"use_cases": ["uc-a", "uc-b"]}).json()["summary"]
    for run_id in run_ids:
        read = client.get(
            f"/runs/{run_id}/artefacts/{ARBITRATION_SUMMARY_FILENAME}",
            headers=_user(app, "reader", Role.VIEWER),
        )
        assert read.status_code == 200, read.text
        assert read.json()["run_ids"] == run_ids == posted["run_ids"]


# ---------------------------------------------------------------------------
# Major: the latest run of a use case is the one that finished last
# ---------------------------------------------------------------------------


def test_the_latest_run_is_chosen_by_finish_time_not_by_run_id(tmp_path: Path) -> None:
    """Fails on cb43c05, which sorted run ids as text: they end in random characters, so on one day the
    older list was used half the time."""
    storage = LocalStorage(tmp_path)
    earlier_id, later_id = "r_20261001_ffffffff", "r_20261001_00000001"
    assert earlier_id > later_id, "the text order is the reverse of the finish order"
    for run_id, rows, finished in ((earlier_id, 50, FINISHED - timedelta(hours=3)), (later_id, 80, FINISHED)):
        write_run(storage, run_id, kind="propensity", rows=rows)
        record = storage.read_model(run_key(run_id, RUN_FILENAME), RunRecord)
        storage.write_model(
            run_key(run_id, RUN_FILENAME),
            record.model_copy(
                update={
                    "use_case_id": "uc-same",
                    "use_case_name": "uc-same",
                    "finished_at": finished,
                    "created_at": finished - timedelta(minutes=5),
                }
            ),
        )
    app = local_app(tmp_path)
    app.state.campaign_store = InMemoryCampaignStore()
    client = TestClient(app, raise_server_exceptions=False)

    named = _post(app, client, {"use_cases": ["uc-same"]})
    assert named.status_code == 200, named.text
    assert named.json()["summary"]["run_ids"] == [later_id]
    assert named.json()["summary"]["total_customers"] == 80
    every = _post(app, client, {})
    assert every.json()["summary"]["run_ids"] == [later_id]


# ---------------------------------------------------------------------------
# Major: a settings file that cannot be read; plain errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["channel_caps:\n  sms: -1\n  email: 500\ncontact_cap_per_customer: 2\n", "not: [valid"],
    ids=["negative-cap", "bad-yaml"],
)
def test_a_settings_file_that_cannot_be_read_is_a_422_with_its_code(tmp_path: Path, text: str) -> None:
    """Fails on cb43c05: the file was ignored whole (the valid email cap too) and the request returned 200."""
    app, client, store, _ = _prepare(
        tmp_path, [("uc-a", {"kind": "propensity", "rows": 30}), ("uc-b", {"kind": "propensity", "rows": 30})]
    )
    root = tmp_path / "config-root"
    (root / "decide").mkdir(parents=True)
    (root / "decide" / "arbitration.yaml").write_text(text, encoding="utf-8")
    app.state.config_root = root

    response = _post(app, client, {"use_cases": ["uc-a", "uc-b"]})

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "ARBITRATION_CONFIG_INVALID"
    assert "fix it or remove it to use the defaults" in detail["message"]
    assert store.list(limit=10) == (), "nothing was arbitrated or measured"
    storage = LocalStorage(tmp_path)
    assert not storage.exists(f"decide/{ARBITRATION_SUMMARY_FILENAME}")


def test_a_run_that_cannot_have_a_treat_list_says_why_in_the_builders_words(tmp_path: Path) -> None:
    """Fails on cb43c05: every failure was RUN_NOT_SCORED with the exception's repr in the message."""
    app, client, _, run_ids = _prepare(
        tmp_path, [("uc-a", {"kind": "propensity", "rows": 30}), ("uc-b", {"kind": "propensity", "rows": 30})]
    )
    storage = LocalStorage(tmp_path)
    record = storage.read_model(run_key(run_ids[1], RUN_FILENAME), RunRecord)
    storage.write_model(
        run_key(run_ids[1], RUN_FILENAME), record.model_copy(update={"state": RunState.RUNNING})
    )

    response = _post(app, client, {"run_ids": run_ids})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "RUN_NOT_SCORED"
    assert "is running" in detail["message"]
    for leak in ("Traceback", "KeyError", "Error(", ".parquet", "runs/"):
        assert leak not in detail["message"]
