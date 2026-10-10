"""Plan J M106 acceptance: learn the next model from the last cycle's randomised rows (DEC-1316).

Everything goes through the product's own API, as a user's files would: a real upload, the uplift
train flow, the uplift score flow (which writes `holdout_assignment.parquet`), step 4's measurement
and step 4's "Learn who to contact next time". Nothing writes a run artefact by hand. The config root is
a copy of `configs/` in which `win-back-campaign` is a campaign-effect use case with an explore slice
of 5%, the smallest the plan names, and `governance.approval_required: false` (the explore world), or
with a control group kept per use case and no explore slice (the no-overlap world). The explore world's
use case lets a model go into use without an Approver, and the learn requests ask nothing about
approval and keep the default champion threshold: the learned model must still wait for an Approver.

**The planted miscalibration.** The model in use (A) is trained on a campaign whose effect was the
reverse of the truth (`effect_scale=-1`) and put in use by hand: it predicts that the customers the
truth calls sleeping dogs gain about 22 points from a contact. It scores a new campaign, which goes
out; the outcomes are drawn from the truth, its effects scaled by 1.5. So the customers on A's list
really lose about 30 points, and the "predicted against measured" block the Approver sees must show
it: A's top tenth predicted far above zero, measured wholly below it.

**What enters the frame.** Only rows whose contact was decided at random: the list's customers
(contacted unless held back) and the customers outside it who could have been explored. In each of
those two groups the smaller side enters whole and the larger side is cut to the same size by a salted
draw on the customer id, so contacted and not-contacted customers are alike in everything but the
contact. Pooling the groups without that cut would make "who was contacted" predictable from the
customers' own data, which the randomness check refuses: the test shows both.

Every test here fails on the commit before M106 (b5ea557): the frame there is the list's own
customers only, there is no learn record and no calibration block, a cycle without an explore slice
learns anyway, the learned model goes straight into use, and an outcomes file covering only the
hand-off is learned from as if it covered everyone.
"""

from __future__ import annotations

import io
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes.uploads import load_upload
from engine.config import ResolvedConfig, load_use_case
from engine.contracts import ModelStatus, RunRecord
from engine.holdout.assign import selection_masks
from engine.pilot.plain import jargon_in
from engine.storage import run_key
from engine.uplift.checks import run_uplift_checks
from engine.uplift.metrics import calibration_by_decile
from engine.utils.ids import seed_from
from tests.fixtures.make_uplift_data import make_uplift_data, make_winback_campaign, outcomes_for
from tests.integration.uplift.test_uplift_api import (
    FAST_OVERRIDES,
    App,
    finish,
    run_artefact,
    scoring_frame,
    start_score,
    start_uplift,
    uplift_body,
    upload,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]  # trains uplift models end to end: 6-10 min serially

USE_CASE: Final[str] = "win-back-campaign"
KEY: Final[str] = "customer_id"
TARGET: Final[str] = "reactivated_90d"
TREATMENT: Final[str] = "contacted"
TRAIN_ROWS: Final[int] = 10_000
CAMPAIGN_ROWS: Final[int] = 60_000
EXPLORE: Final[float] = 0.05
EFFECT: Final[float] = 1.5
"""The campaign's true effects, scaled up so the learned model's ranking is measurably better than chance."""
SALT: Final[str] = "learn-from-cycle-salt-0001"
TRAIN_RUN: Final[str] = "r_20261009_6a000001"
EXPLORE_RUN: Final[str] = "r_20261009_6a000002"
CLOSED_RUN: Final[str] = "r_20261009_6a000003"
HANDOFF_RUN: Final[str] = "r_20261009_6a000004"
LEARN_OVERRIDES: Final[dict[str, Any]] = {"uplift": dict(FAST_OVERRIDES["uplift"])}
"""Seconds, not minutes. Nothing about approval and no champion threshold: the defaults hold."""


def make_root(
    config_root: Path, target: Path, *, actions: dict[str, Any], governance: dict[str, Any] | None = None
) -> Path:
    """A copy of `configs/` whose `win-back-campaign` is a campaign-effect use case with `actions` added."""
    shutil.copytree(config_root, target)
    path = target / "use_cases" / "win_back_campaign.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["problem_type"] = "uplift"
    document["model_search"] = {"metric": "auuc", "metric_choices": ["auuc"]}
    document.setdefault("actions", {}).update(actions)
    if governance is not None:
        document.setdefault("governance", {}).update(governance)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return target


@dataclass(frozen=True)
class Cycle:
    app: App
    run_id: str
    record: RunRecord
    scores: pd.DataFrame
    """`scores.parquet`, keyed by customer as text."""
    table: pd.DataFrame
    """`holdout_assignment.parquet`, keyed by customer as text."""
    outcomes: pd.DataFrame
    outcomes_upload: str


@dataclass(frozen=True)
class Learned:
    cycle: Cycle
    model_in_use: RunRecord
    uplift_run: RunRecord
    experiment: pd.DataFrame


def _read_parquet(app: App, run_id: str, name: str) -> pd.DataFrame:
    frame = pd.read_parquet(io.BytesIO(run_artefact(app, run_id, name)))
    frame[KEY] = frame[KEY].astype(str)
    return frame.set_index(KEY, drop=False)


def _cycle(app: App, run_id: str, *, seed: int, handoff_only: bool = False) -> Cycle:
    """Score a new campaign with the model in use, send it, and measure it through step 4.

    `handoff_only` keeps outcomes for the hand-off alone (the list, its control group and the explored
    customers), as a campaign tool's export would: nobody else outside the list has one.
    """
    campaign = make_winback_campaign(CAMPAIGN_ROWS, seed=seed)
    record = finish(
        app,
        start_score(
            app,
            {"upload_id": upload(app, scoring_frame(campaign), mode="score"), "primary_key": KEY},
            run_id=run_id,
        ),
    )
    scores = _read_parquet(app, run_id, "scores.parquet")
    table = _read_parquet(app, run_id, "holdout_assignment.parquet")
    treated = set(table.index[table["treated"].to_numpy(dtype=bool)])
    outcomes = outcomes_for(campaign, treated_keys=treated, seed=seed + 1, effect_scale=EFFECT)
    if handoff_only:
        config = app.storage.read_model(run_key(run_id, "run_config.json"), ResolvedConfig).config
        selected, _sleeping = selection_masks(scores.reset_index(drop=True), config)
        handed = set(scores.index[selected]) | treated
        outcomes = outcomes[outcomes[KEY].astype(str).isin(handed)].reset_index(drop=True)
    outcomes_upload = upload(app, outcomes, mode="score")
    # The runs finish today, so the outcomes are read as final at once (a window of 0 days): this test is
    # about which customers the next model learns from, not about waiting for outcomes.
    measured = app.client.post(
        f"/runs/{run_id}/measure", json={"upload_id": outcomes_upload, "outcome_window_days": 0}
    )
    assert measured.status_code == 200, measured.text
    return Cycle(
        app, run_id, record, scores, table, outcomes.set_index(outcomes[KEY].astype(str)), outcomes_upload
    )


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("learn-from-cycle") / "data"
    path.mkdir()
    return path


@pytest.fixture(scope="module")
def explore_root(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_root(
        config_root,
        tmp_path_factory.mktemp("learn-explore-root") / "configs",
        actions={"explore_fraction": EXPLORE},
        governance={"approval_required": False},
    )


@pytest.fixture(scope="module")
def learned(explore_root: Path, data_dir: Path) -> Iterator[Learned]:
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("MARKETING_AI_CONFIG_DIR", raising=False)
        with TestClient(create_app(config_root=explore_root, data_dir=data_dir)) as client:
            app = App(client=client, data_dir=data_dir)
            reversed_world = make_uplift_data(TRAIN_ROWS, seed=7, effect_scale=-1.0).frame
            model_in_use = finish(
                app,
                start_uplift(app, uplift_body(upload(app, reversed_world, mode="train")), run_id=TRAIN_RUN),
            )
            # Its own hold-out cannot show it beats chance, so a person puts it in use by hand, as the
            # Models page lets them (DEC-609): the planted model in use.
            promoted = client.post(
                f"/models/{model_in_use.model_version_id}/promote",
                json={"promoted_by": "learn test", "reason": "the model in use for this test"},
            )
            assert promoted.status_code == 200, promoted.text
            cycle = _cycle(app, EXPLORE_RUN, seed=11)
            response = client.post(f"/runs/{EXPLORE_RUN}/measure/learn", json={"overrides": LEARN_OVERRIDES})
            assert response.status_code == 202, response.text
            uplift_run = finish(app, str(response.json()["run_id"]))
            assert uplift_run.upload_id is not None
            experiment = pd.read_parquet(
                app.storage.local_path(load_upload(app.storage, uplift_run.upload_id).source_key)
            )
            experiment[KEY] = experiment[KEY].astype(str)
            yield Learned(cycle, model_in_use, uplift_run, experiment.set_index(KEY, drop=False))


def _groups(learned: Learned) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Per scored customer: eligible, on the list (selected before the hold-back), predicted sleeping dog."""
    cycle = learned.cycle
    config = cycle.app.storage.read_model(run_key(cycle.run_id, "run_config.json"), ResolvedConfig).config
    selected, sleeping = selection_masks(cycle.scores.reset_index(drop=True), config)
    index = cycle.scores.index
    eligible = cycle.scores["suppressed_reason"].isna()
    return eligible, pd.Series(selected, index=index), pd.Series(sleeping, index=index)


# ---------------------------------------------------------------------------
# The frame
# ---------------------------------------------------------------------------
def test_only_randomised_rows_enter_and_the_treatment_is_the_logged_contact(learned: Learned) -> None:
    frame, table = learned.experiment, learned.cycle.table
    eligible, selected, sleeping = _groups(learned)
    keys = frame.index
    probability = table.loc[keys, "treatment_probability"]
    assert (
        (probability > 0.0) & (probability < 1.0)
    ).all(), "a row whose contact was not left to chance entered"
    assert eligible.loc[keys].all() and not sleeping.loc[keys].any()
    assert (frame[TREATMENT].to_numpy() == table.loc[keys, "treated"].to_numpy(dtype=int)).all()
    assert (frame[TARGET].to_numpy() == learned.cycle.outcomes.loc[keys, TARGET].to_numpy()).all()
    outside = ~selected.loc[keys]
    assert (
        outside.any()
    ), "the customers outside the list (the explore slice and its comparison) never entered"
    assert table.loc[keys[outside.to_numpy()], "explore"].any()


def test_in_each_group_the_smaller_side_enters_whole_and_the_larger_is_cut_to_its_size(
    learned: Learned,
) -> None:
    frame, table = learned.experiment, learned.cycle.table
    _eligible, selected, _sleeping = _groups(learned)
    probability = table["treatment_probability"]
    randomised = table.index[((probability > 0.0) & (probability < 1.0)).to_numpy()]
    for on_list in (True, False):
        group = randomised[(selected.loc[randomised] == on_list).to_numpy()]
        contacted = table.loc[group, "treated"].to_numpy(dtype=bool)
        smaller = group[contacted] if contacted.sum() <= (~contacted).sum() else group[~contacted]
        entered = frame.index.intersection(group)
        assert len(entered) == 2 * len(smaller), on_list
        assert set(smaller) <= set(entered), on_list
        assert int(frame.loc[entered, TREATMENT].sum()) == len(smaller), on_list


def test_the_frame_passes_the_randomness_check_and_the_unbalanced_pool_would_not(learned: Learned) -> None:
    app = learned.cycle.app
    checked = app.client.get(f"/runs/{learned.uplift_run.run_id}/uplift/uplift_validation.json")
    assert checked.status_code == 200, checked.text
    report = checked.json()
    assert report["passed"] is True and report["causal"] is True
    assert "TREATMENT_NOT_RANDOM" not in {check["code"] for check in report["checks"]}
    assert report["randomness_auc"] is not None and report["randomness_auc"] <= 0.60

    # The same randomised rows, every one of them, with no cut: contact is then predictable from the
    # customers' own data (the list's customers were contacted nine times in ten, the others about one in 22).
    table, scores = learned.cycle.table, learned.cycle.scores
    probability = table["treatment_probability"]
    pooled_keys = table.index[((probability > 0.0) & (probability < 1.0)).to_numpy()]
    inputs = scoring_frame(make_winback_campaign(CAMPAIGN_ROWS, seed=11))
    inputs[KEY] = inputs[KEY].astype(str)
    pooled = inputs.set_index(KEY, drop=False).loc[pooled_keys].reset_index(drop=True)
    pooled[TREATMENT] = table.loc[pooled_keys, "treated"].to_numpy(dtype=int)
    pooled[TARGET] = learned.cycle.outcomes.loc[pooled_keys, TARGET].to_numpy()
    config = load_use_case(USE_CASE)
    config = config.model_copy(
        update={
            "uplift": config.uplift.model_copy(
                update={"treatment_column": TREATMENT, "min_arm_rows": 200, "min_arm_positives": 20}
            )
        }
    )
    unbalanced = run_uplift_checks(pooled, config, primary_key=KEY, target=TARGET, upload_id="pooled", seed=1)
    assert "TREATMENT_NOT_RANDOM" in {check.code for check in unbalanced.report.checks}
    assert len(scores.index) == CAMPAIGN_ROWS


def test_step_4_records_which_rows_entered_and_why(learned: Learned) -> None:
    app, cycle = learned.cycle.app, learned.cycle
    view = app.client.get(f"/runs/{cycle.run_id}/measure").json()
    record = view["learned"]
    assert record["source_run_id"] == cycle.run_id and record["uplift_run_id"] == learned.uplift_run.run_id
    eligible, selected, sleeping = _groups(learned)
    frame = learned.experiment
    assert record["rows"] == len(frame.index)
    assert record["contacted"] == int(frame[TREATMENT].sum()) == record["rows"] // 2
    groups = {group["group"]: group for group in record["groups"]}
    assert set(groups) == {"on_the_list", "outside_the_list"}
    on_list = frame.index[selected.loc[frame.index].to_numpy()]
    assert groups["on_the_list"]["entered_contacted"] + groups["on_the_list"]["entered_not_contacted"] == len(
        on_list
    )
    assert groups["outside_the_list"]["chance_of_contact"] == pytest.approx((1 - 0.10) * EXPLORE)
    for group in groups.values():
        # The outcomes file covers everyone, so nobody is left out for want of an outcome.
        assert group["outcome_coverage_contacted"] == group["outcome_coverage_not_contacted"] == 1.0
    left = record["left_out"]
    assert left["not_eligible"] == int((~eligible).sum())
    assert left["never_contacted_by_the_rule"] == int((eligible & sleeping).sum())
    assert left["no_chance_of_contact"] == 0
    assert left["cut_to_balance"] == (
        int(((cycle.table["treatment_probability"] > 0) & (cycle.table["treatment_probability"] < 1)).sum())
        - left["no_outcome"]
        - record["rows"]
    )
    for sentence in (record["summary"], *(note for note in record["notes"])):
        assert jargon_in(sentence) == (), sentence


# ---------------------------------------------------------------------------
# The Approver's block
# ---------------------------------------------------------------------------
def test_the_learned_model_waits_for_an_approver_although_the_use_case_needs_none(learned: Learned) -> None:
    app = learned.cycle.app
    scored = app.storage.read_model(run_key(learned.cycle.run_id, "run_config.json"), ResolvedConfig)
    assert scored.config.governance.approval_required is False, "the use case itself needs no Approver"
    assert scored.sources["governance.approval_required"] == "use_case"
    config = app.storage.read_model(run_key(learned.uplift_run.run_id, "run_config.json"), ResolvedConfig)
    assert config.config.governance.approval_required is True
    assert config.sources["governance.approval_required"] == "override"
    challenger = app.registry.get(learned.uplift_run.model_version_id or "")
    assert challenger.status is ModelStatus.PENDING_APPROVAL, "a learned model went into use unapproved"
    in_use = app.registry.get(learned.model_in_use.model_version_id or "")
    assert in_use.status is ModelStatus.CHAMPION


def test_a_learn_request_cannot_switch_the_approval_off(learned: Learned) -> None:
    app, cycle = learned.cycle.app, learned.cycle
    before = {path.name for path in (app.data_dir / "runs").iterdir()}
    for overrides in (
        {**LEARN_OVERRIDES, "governance": {"approval_required": False}},
        {**LEARN_OVERRIDES, "governance.approval_required": False},
    ):
        refused = app.client.post(f"/runs/{cycle.run_id}/measure/learn", json={"overrides": overrides})
        assert refused.status_code == 422, refused.text
        assert "Approver" in refused.json()["detail"]["message"]
    assert {path.name for path in (app.data_dir / "runs").iterdir()} == before


def test_the_approver_sees_the_planted_miscalibration_of_the_model_in_use(learned: Learned) -> None:
    app, cycle = learned.cycle.app, learned.cycle
    challenger = app.registry.get(learned.uplift_run.model_version_id or "")
    # At the default champion threshold the learned model is put forward, so the block reaches the
    # Approver; one that did not clear it would stay a candidate, its block on step 4's record only.
    assert challenger.status is ModelStatus.PENDING_APPROVAL
    body = app.client.get("/approvals").json()
    item = next(item for item in body["items"] if item["version"]["model_id"] == challenger.model_id)
    block = item["live_calibration"]
    assert block["source_run_id"] == cycle.run_id
    assert block["model_id"] == learned.model_in_use.model_version_id
    assert block["matches"] is False

    # The numbers are the campaign's own: A's predictions for the rows the frame holds, against what the
    # campaign measured in each tenth, with the run's own resamples.
    frame = learned.experiment
    config = app.storage.read_model(
        run_key(learned.uplift_run.run_id, "run_config.json"), ResolvedConfig
    ).config
    expected = calibration_by_decile(
        cycle.scores.loc[frame.index, "uplift"].to_numpy(dtype=float),
        frame[TREATMENT].to_numpy(),
        frame[TARGET].to_numpy(),
        samples=config.uplift.bootstrap_samples,
        seed=seed_from(cycle.run_id),
    )
    assert [d["predicted"] for d in block["deciles"]] == pytest.approx(
        [d.predicted_uplift for d in expected.deciles]
    )
    assert [d["measured"]["value"] for d in block["deciles"]] == pytest.approx(
        [d.observed_uplift.value for d in expected.deciles if d.observed_uplift is not None]
    )
    top = block["deciles"][0]
    assert top["predicted"] > 0.10, "A predicts a large gain for the customers on its list"
    assert top["measured"]["ci_high"] < 0.0, "the campaign measured a loss there"
    assert top["inside_range"] is False
    assert jargon_in(block["summary"]) == (), block["summary"]


# ---------------------------------------------------------------------------
# A cycle that left part of its customers no chance of contact
# ---------------------------------------------------------------------------
def test_a_cycle_without_an_explore_slice_is_refused_with_the_reason(
    learned: Learned, config_root: Path, tmp_path_factory: pytest.TempPathFactory, data_dir: Path
) -> None:
    root = make_root(
        config_root,
        tmp_path_factory.mktemp("learn-closed-root") / "configs",
        actions={"holdout": {"scope": "use_case", "fraction": 0.10}},
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("MARKETING_AI_CONFIG_DIR", raising=False)
        patch.setenv("MARKETING_AI_HOLDOUT_SALT", SALT)
        with TestClient(create_app(config_root=root, data_dir=data_dir)) as client:
            app = App(client=client, data_dir=data_dir)
            cycle = _cycle(app, CLOSED_RUN, seed=21)
            before = {path.name for path in (data_dir / "runs").iterdir()}
            view = client.get(f"/runs/{CLOSED_RUN}/measure").json()
            assert view["learn"]["ready"] is False
            refused = client.post(f"/runs/{CLOSED_RUN}/measure/learn", json={"overrides": LEARN_OVERRIDES})
            assert refused.status_code == 409, refused.text
            detail = refused.json()["detail"]
            assert detail["code"] == "LEARN_NO_OVERLAP"
            assert detail["message"] == view["learn"]["reason"]
            assert jargon_in(detail["message"]) == (), detail["message"]
            assert "outside" in detail["message"]
            assert {
                path.name for path in (data_dir / "runs").iterdir()
            } == before, "a refused learn started a run"
            assert cycle.table["explore"].sum() == 0


# ---------------------------------------------------------------------------
# An outcomes file that undoes the randomisation
# ---------------------------------------------------------------------------
def test_an_outcomes_file_covering_only_the_hand_off_is_refused(learned: Learned) -> None:
    """Outcomes for the list, its control group and the explored customers only: outside the list every
    customer with an outcome was contacted, so dropping the rest would leave nothing to compare with."""
    app = learned.cycle.app
    cycle = _cycle(app, HANDOFF_RUN, seed=31, handoff_only=True)
    explored = cycle.table["explore"].to_numpy(dtype=bool)
    assert explored.any() and cycle.table.index[explored].isin(cycle.outcomes.index).all()
    before = {path.name for path in (app.data_dir / "runs").iterdir()}
    refused = app.client.post(f"/runs/{HANDOFF_RUN}/measure/learn", json={"overrides": LEARN_OVERRIDES})
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "LEARN_NO_OVERLAP"
    assert "outside the list" in detail["message"] and "0%" in detail["message"]
    assert jargon_in(detail["message"]) == (), detail["message"]
    assert {
        path.name for path in (app.data_dir / "runs").iterdir()
    } == before, "a refused learn started a run"
