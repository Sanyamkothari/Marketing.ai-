"""MineThatData e-mail test (Hillstrom): the data folder, the use case, and the whole Plan J journey (M110).

Nothing here downloads anything. The fast tests read the committed `sample.csv`, `mapping.yaml` and the use
case. The journey test runs every step of `library.journey.run_journey` on the sample through the product's
own API, with smaller arm floors (a tenth of the file has about thirty conversions); it checks that each
step produced what it should and that nothing was fitted on the evaluation rows, not what the numbers are.
The numbers are the full file's, in `run_report.md`; the two tests that compare with them skip, saying
why, when the full file or its run is absent (it is never committed: see the dataset's README).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml

from conftest import CONFIGS, LIBRARY, REPO_ROOT

FOLDER = LIBRARY / "hillstrom-email"
SAMPLE = FOLDER / "sample.csv"
PREPARED = FOLDER / "data" / "prepared.csv"
RESULTS = LIBRARY / ".runs" / "hillstrom-email" / "journey" / "journey.results.json"
REPORT = FOLDER / "run_report.md"

SAMPLE_FLOORS: dict[str, Any] = {
    "validation.min_positive": 20,
    "uplift.min_arm_positives": 5,
    "uplift.min_arm_rows": 500,
    "uplift.bootstrap_samples": 50,
}
"""The sample is a tenth of the file, so it has a tenth of the conversions: the floors a sample needs."""


def _fetch() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location("hillstrom_fetch", FOLDER / "fetch.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# The data folder
# ---------------------------------------------------------------------------
def test_fetch_refuses_a_file_that_is_not_the_validated_one(tmp_path: Path) -> None:
    fetch = _fetch()
    other = tmp_path / "hillstrom.csv"
    other.write_text("recency,history\n1,2\n", encoding="utf-8")
    with pytest.raises(fetch.ChecksumError):
        fetch.verify(other)
    assert fetch.sha256_of(other) == hashlib.sha256(other.read_bytes()).hexdigest()
    assert fetch.CANONICAL_URL.startswith("http://www.minethatdata.com/")
    assert fetch.MIRROR_URL.endswith("hillstorm_no_indices.csv.gz")


def test_the_full_file_is_never_committed() -> None:
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "library/hillstrom-email"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "library/hillstrom-email/sample.csv" in tracked
    assert not [name for name in tracked if "/data/" in name], tracked
    assert sum(1 for _ in SAMPLE.open(encoding="utf-8")) - 1 < 64_000 // 5


def test_the_sample_is_a_tenth_of_each_group_with_the_published_columns() -> None:
    fetch = _fetch()
    sample = pd.read_csv(SAMPLE)
    assert tuple(sample.columns) == (fetch.PRIMARY_KEY, *fetch.COLUMNS)
    assert sample[fetch.PRIMARY_KEY].is_unique
    counts = sample[fetch.TREATMENT].value_counts()
    assert set(counts.index) == set(fetch.LEVELS)
    assert all(abs(count - fetch.SAMPLE_ROWS / 3) < 0.02 * fetch.SAMPLE_ROWS for count in counts)


def test_the_sample_rows_are_the_full_files_rows() -> None:
    if not PREPARED.is_file():
        pytest.skip(
            f"{PREPARED} is absent (never committed); run library/hillstrom-email/fetch.py to check this"
        )
    fetch = _fetch()
    full = pd.read_csv(PREPARED).set_index(fetch.PRIMARY_KEY)
    sample = pd.read_csv(SAMPLE).set_index(fetch.PRIMARY_KEY)
    pd.testing.assert_frame_equal(full.loc[sample.index], sample)
    rebuilt = fetch.write_sample(full.reset_index(), path=Path("/dev/null"))
    assert list(rebuilt[fetch.PRIMARY_KEY]) == list(sample.index), "fetch.py draws the committed sample"


def test_mapping_and_use_case_agree_and_the_template_examples_are_real_rows() -> None:
    from engine.config import load_use_case

    mapping = yaml.safe_load((FOLDER / "mapping.yaml").read_text(encoding="utf-8"))
    config = load_use_case("hillstrom-email", CONFIGS)
    roles = mapping["roles"]
    assert config.target.column == roles["target"] == "conversion"
    assert config.uplift.treatment_column == roles["treatment"] == "segment"
    excluded = {c["source"] for c in mapping["columns"] if c["role"] in {"excluded", "treatment"}}
    assert excluded <= set(config.prepare.exclude_columns), "outcomes and the lever are never features"
    assert mapping["pii"] == []
    examples = pd.DataFrame({c.name: list(c.examples) for c in config.template.columns})
    if not PREPARED.is_file():
        pytest.skip("the template examples are checked against the full file, which is absent")
    full = pd.read_csv(PREPARED, dtype=str, keep_default_na=False).head(5)
    pd.testing.assert_frame_equal(examples[full.columns].reset_index(drop=True), full)


def test_the_journey_overrides_resolve_into_a_three_level_campaign_effect_run() -> None:
    from library.journey import HILLSTROM

    from engine.config import resolve_config

    overrides = {"problem_type": "uplift", "uplift.treatment_column": "segment", **HILLSTROM.uplift_overrides}
    config = resolve_config("hillstrom-email", overrides, root=CONFIGS).config
    assert config.uplift.multi_arm and len(config.uplift.treatment_levels) == 3
    assert config.uplift.policy.value_column == HILLSTROM.value_column
    risk = resolve_config("hillstrom-email", HILLSTROM.risk_overrides, root=CONFIGS).config
    assert risk.problem_type.value == "binary_classification"


# ---------------------------------------------------------------------------
# The off-policy estimate, by hand
# ---------------------------------------------------------------------------
def test_the_inverse_probability_value_is_the_matched_rows_outcome_over_their_arm_share() -> None:
    from library.journey import ips_value, off_policy

    logged = np.array([0, 1, 2, 0, 1, 2])
    y = np.array([0.0, 1.0, 0.0, 1.0, 1.0, 0.0])
    shares = np.array([1 / 3, 1 / 3, 1 / 3])
    everyone_one = np.ones(6, dtype=np.int_)
    terms = ips_value(logged, y, everyone_one, shares)
    assert terms.tolist() == pytest.approx([0, 3, 0, 0, 3, 0])
    assert terms.mean() == pytest.approx(1.0)  # both arm-1 rows converted: a rate of 1
    nobody = np.zeros(6, dtype=np.int_)
    found = off_policy(
        logged, {"y": y}, {"nobody": nobody, "one": everyone_one}, baseline="nobody", shares=shares
    )
    assert found["y"]["nobody"]["value"]["value"] == pytest.approx(0.5)
    assert found["y"]["one"]["difference_from_nobody"]["value"] == pytest.approx(0.5)
    assert (
        found["y"]["one"]["difference_from_nobody"]["ci_low"]
        < 0.5
        < found["y"]["one"]["difference_from_nobody"]["ci_high"]
    )


def test_the_split_is_seeded_stratified_and_disjoint() -> None:
    from library.journey import HILLSTROM, split_rows

    sample = pd.read_csv(SAMPLE)
    train, evaluation = split_rows(sample, HILLSTROM)
    again, _ = split_rows(sample, HILLSTROM)
    assert list(train["customer_id"]) == list(again["customer_id"])
    assert not set(train["customer_id"]) & set(evaluation["customer_id"])
    assert len(train) + len(evaluation) == len(sample)
    for level in HILLSTROM.levels:
        both = (sample["segment"] == level) & (sample["conversion"] == 1)
        kept = (evaluation["segment"] == level) & (evaluation["conversion"] == 1)
        assert abs(int(kept.sum()) - int(both.sum()) / 2) <= 1


# ---------------------------------------------------------------------------
# The whole journey, on the sample
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def journey(tmp_path_factory: pytest.TempPathFactory) -> Any:
    from library.journey import HILLSTROM, run_journey

    return run_journey(
        HILLSTROM,
        csv_path=SAMPLE,
        config_root=CONFIGS,
        runs_dir=tmp_path_factory.mktemp("runs"),
        extra_risk_overrides={"validation.min_positive": 20},
        extra_uplift_overrides=SAMPLE_FLOORS,
    )


@pytest.mark.slow
@pytest.mark.integration
def test_every_step_of_the_journey_ran_through_the_api(journey: Any) -> None:
    steps = journey.results["steps"]
    assert steps["risk_model"]["state"] == "done"
    uplift = steps["uplift_model"]
    assert (
        uplift["state"] == "done" and uplift["model_status"] == "candidate"
    ), "several offers are never champion"
    assert [arm["arm"] for arm in uplift["evaluation"]["arms"]] == ["Mens E-Mail", "Womens E-Mail"]
    assert uplift["arm_policy_value"] is not None
    codes = {c["code"] for c in steps["approval"]["checks"]}
    assert codes == {"UPLIFT_NOT_BETTER_THAN_RISK", "UPLIFT_UNSTABLE_ACROSS_FOLDS", "UPLIFT_MISCALIBRATED"}
    assert all(c["passed"] is not None for c in steps["approval"]["checks"]), "each check was measured"
    choice = steps["treat_list"]["offer_choice"]
    assert choice["chosen"] is True and choice["value_column"] == "value_inr"
    assert steps["treat_list"]["treat_rows"] == sum(arm["offered_rows"] for arm in choice["arms"])
    for outcome in ("conversion", "visit", "spend"):
        chosen = steps["off_policy"]["estimates"][outcome]["chosen"]["difference_from_no_email"]
        assert chosen["ci_low"] is not None and chosen["ci_low"] <= chosen["value"] <= chosen["ci_high"]
    assert steps["power"]["evaluation_rows"]["preview"]["points"]


@pytest.mark.slow
@pytest.mark.integration
def test_the_campaigns_are_measured_on_replayed_rows_and_their_packs_are_traced(journey: Any) -> None:
    from engine.pilot.proof import ProofView, verify_provenance
    from engine.storage import LocalStorage

    storage = LocalStorage(journey.directory / "data")
    for outcome, campaign in journey.results["steps"]["campaigns"].items():
        report, replay = campaign["report"], campaign["replay"]
        assert "public dataset, retrospective" in campaign["name"]
        assert (
            report["treated_rows"] == replay["kept_treated"]
            and report["control_rows"] == replay["kept_held_back"]
        )
        assert (
            report["rows_without_outcome"]
            == replay["intended"] - replay["kept_treated"] - replay["kept_held_back"]
        )
        assert (
            report["test_plan_hash"] == campaign["plan"]["plan_hash"]
        ), "measured against the plan registered first"
        assert campaign["proof"]["status"] == 200 and campaign["proof"]["provenance_verified"] is True
        view = ProofView.model_validate(campaign["proof"]["view"])
        verify_provenance(view, storage)
        assert view.campaign_name is not None and "retrospective" in view.campaign_name.text
        if outcome == "spend":
            assert report["outcome_kind"] == "continuous" and report["covariate_column"] == "history"
            assert report["adjusted_interval"] is not None, "the registered covariate was used"
        assert (journey.directory / f"proof_{outcome}.html").stat().st_size > 0


@pytest.mark.slow
@pytest.mark.integration
def test_nothing_was_fitted_on_the_evaluation_rows(journey: Any) -> None:
    """The training upload holds training rows only, and the scoring upload no e-mail group and no outcome."""
    data = journey.directory / "data" / "uploads"
    frames = {path.parent.name: pd.read_csv(path) for path in data.glob("*/source.csv")}
    train_id = next(k for k, f in frames.items() if "conversion" in f.columns and "segment" in f.columns)
    score_id = next(k for k, f in frames.items() if "segment" not in f.columns and "value_inr" in f.columns)
    evaluation_ids = set(frames[score_id]["customer_id"])
    assert not evaluation_ids & set(frames[train_id]["customer_id"])
    assert not {"segment", "visit", "conversion", "spend"} & set(frames[score_id].columns)
    split = journey.results["split"]
    assert len(evaluation_ids) == split["evaluation"]["rows"]
    assert len(frames[train_id]) == split["training"]["rows"]


@pytest.mark.slow
@pytest.mark.integration
def test_the_report_is_rendered_from_the_results_alone(journey: Any) -> None:
    from library.journey_report import render_report

    first = render_report(journey.results, results_path="r.json", command="cmd")
    second = render_report(
        json.loads(json.dumps(journey.results, default=str)), results_path="r.json", command="cmd"
    )
    assert first == second
    assert "Public dataset, retrospective" in first and "## 7. Assumptions and settings" in first


# ---------------------------------------------------------------------------
# The committed report against the full file's run
# ---------------------------------------------------------------------------
def test_the_committed_report_is_what_run_engine_renders_from_its_run() -> None:
    if not RESULTS.is_file():
        pytest.skip(
            f"{RESULTS} is absent: run python -m library.run_engine journey --dataset hillstrom-email"
        )
    from library.run_engine import journey_report_text

    assert REPORT.read_text(encoding="utf-8") == journey_report_text(
        json.loads(RESULTS.read_text(encoding="utf-8"))
    )


def _numbers(results: dict[str, Any]) -> dict[str, Any]:
    """Every number of a journey that a re-run on the same file must reproduce (not times, not random ids)."""
    steps = results["steps"]
    campaigns = {
        outcome: {
            "report": {
                key: campaign["report"].get(key)
                for key in (
                    "treated_rows",
                    "control_rows",
                    "absolute_lift",
                    "mean_difference_ci",
                    "adjusted_interval",
                )
            },
            "headline": campaign["proof"]["view"]["headline"],
        }
        for outcome, campaign in steps["campaigns"].items()
    }
    return {
        "split": results["split"],
        "risk": steps["risk_model"]["test_metrics"],
        "auuc": steps["uplift_model"]["evaluation"]["auuc"],
        "arms": [(arm["effect"], arm["auuc"]) for arm in steps["uplift_model"]["evaluation"]["arms"]],
        "approval": steps["approval"]["checks"],
        "offers": steps["treat_list"]["offer_counts"],
        "off_policy": steps["off_policy"]["estimates"],
        "campaigns": campaigns,
    }


@pytest.mark.slow
@pytest.mark.integration
def test_a_fresh_run_on_the_full_file_reproduces_the_committed_numbers(tmp_path: Path) -> None:
    if not (PREPARED.is_file() and RESULTS.is_file()):
        pytest.skip(
            "needs the full file (fetch.py) and its committed run (run_engine journey); neither is in git"
        )
    from library.journey import HILLSTROM, run_journey

    fresh = run_journey(HILLSTROM, csv_path=PREPARED, config_root=CONFIGS, runs_dir=tmp_path)
    committed = json.loads(RESULTS.read_text(encoding="utf-8"))
    def same_shape(numbers: dict[str, Any]) -> Any:
        return json.loads(json.dumps(numbers, default=str))

    assert same_shape(_numbers(fresh.results)) == same_shape(_numbers(committed))
