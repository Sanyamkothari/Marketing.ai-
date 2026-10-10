"""Plan K M137 end to end: "Safe to roll out" in a real training run's `risk_comparison.json`.

Two LightGBM uplift models are trained through `Pipeline.run_train`, as M96's
`tests/integration/decide/test_uplift_ranking_flow.py` trains one (3 folds, seconds), on the same file,
with the same run id (the run's seed is drawn from it) in two data directories:

* **default** - `uplift.evidence.risk_comparison` on, `rollout_verdict` left at its default (off):
  `risk_comparison.json` carries no `rollout` key and, timestamps aside, is byte-identical to the file
  the code before M137 wrote for the same run (`tests/fixtures/decide/golden/
  risk_comparison_default.json`, written on the unchanged code by `python -m
  tests.integration.uplift.test_rollout_verdict_flow --update` before M137 landed);
* **rollout** - the same plus `rollout_verdict: true` and M97's value inputs
  (`uplift.policy.value_column: monthly_spend`, a 30% margin, 6 months; three customers have no
  `monthly_spend`): the file carries `rollout`, whose conversion bounds are the one-sided bounds of the
  very paired difference M96 reports, in rupees on M97's basis, and everything else in the file is
  what the default run wrote.

Then the verdict's sentence is on the Approver's check, beside the comparison's sentence, in plain words.
Fails on the code before M137 (no `rollout_verdict` setting).
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from statistics import NormalDist
from typing import Any

import pandas as pd
import pytest

from engine.config import RunMode, resolve_config
from engine.contracts import ModelVersion, RunState
from engine.jobs import CancelToken, NullJobRunner
from engine.pipeline import Pipeline, StageContext
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.storage import LocalStorage, run_key, upload_key
from engine.uplift.contracts import UPLIFT_NOT_BETTER_THAN_RISK, RiskComparison
from tests.fixtures.make_uplift_data import make_uplift_data

pytestmark = pytest.mark.integration

USE_CASE = "win-back-campaign"
TARGET = "reactivated_90d"
TREATMENT = "treatment"
TRAIN_ROWS = 5_000
VALUE = "monthly_spend"
MISSING_VALUE_ROWS = (5, 17, 404)
TRAIN_RUN = "r_20261010_0d137001"
UPLOAD_ID = "u_0d137001"
RISK_COMPARISON = "risk_comparison.json"
GOLDEN = (
    Path(__file__).resolve().parents[2] / "fixtures" / "decide" / "golden" / "risk_comparison_default.json"
)
M96_OVERRIDES: dict[str, Any] = {
    "problem_type": "uplift",
    "uplift.treatment_column": TREATMENT,
    "uplift.base_model": "lightgbm",
    "uplift.bootstrap_samples": 50,
    "uplift.min_arm_rows": 200,
    "uplift.min_arm_positives": 20,
    "uplift.evidence.risk_comparison": True,
    "uplift.evidence.folds": 3,
    "uplift.policy.budget_contacts": 80,
    "uplift.policy.cost_per_contact": 20.0,
    "governance.approval_required": False,
}
ROLLOUT_OVERRIDES: dict[str, Any] = {
    **M96_OVERRIDES,
    "uplift.evidence.rollout_verdict": True,
    "uplift.policy.value_column": VALUE,
    "uplift.policy.margin_pct": 30.0,
    "uplift.policy.horizon_months": 6,
}
_TIMESTAMP = re.compile(r'"(computed_at|created_at)": "[^"]+"')


def training_frame() -> pd.DataFrame:
    frame = make_uplift_data(TRAIN_ROWS, seed=7).frame.copy()
    frame.loc[list(MISSING_VALUE_ROWS), VALUE] = float("nan")
    return frame


def without_timestamps(text: str) -> str:
    return _TIMESTAMP.sub(r'"\1": "<time>"', text)


def train(data_dir: Path, overrides: dict[str, Any], config_root: Path) -> None:
    resolved = resolve_config(USE_CASE, overrides, root=config_root)
    storage = LocalStorage(data_dir)
    upload = upload_key(UPLOAD_ID, "source.csv")
    storage.write_bytes(upload, training_frame().to_csv(index=False, lineterminator="\n").encode("utf-8"))
    registry = LocalModelRegistry(data_dir / REGISTRY_FILENAME)
    context = StageContext(
        run_id=TRAIN_RUN,
        mode=RunMode.TRAIN,
        config=resolved.config,
        resolved=resolved,
        storage=storage,
        registry=registry,
        cancel=CancelToken(),
        primary_key=["customer_id"],
        target=TARGET,
        upload_key=upload,
        model_version_id=None,
    )
    record = Pipeline(storage, registry, NullJobRunner()).run_train(context)
    assert record.state is RunState.DONE, record.error


@dataclass(frozen=True)
class Run:
    data_dir: Path

    @property
    def storage(self) -> LocalStorage:
        return LocalStorage(self.data_dir)

    def text(self) -> str:
        return self.storage.read_bytes(run_key(TRAIN_RUN, RISK_COMPARISON)).decode("utf-8")

    def report(self) -> RiskComparison:
        return self.storage.read_model(run_key(TRAIN_RUN, RISK_COMPARISON), RiskComparison)

    def version(self) -> ModelVersion:
        registry = LocalModelRegistry(self.data_dir / REGISTRY_FILENAME)
        return next(v for v in registry.list_versions(USE_CASE) if v.run_id == TRAIN_RUN)


@dataclass(frozen=True)
class Runs:
    default: Run
    rollout: Run


@pytest.fixture(scope="module")
def runs(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runs]:
    root = tmp_path_factory.mktemp("rollout-m137")
    dirs = {name: root / name / "data" for name in ("default", "rollout")}
    for data_dir in dirs.values():
        data_dir.mkdir(parents=True)
    train(dirs["default"], M96_OVERRIDES, config_root)
    train(dirs["rollout"], ROLLOUT_OVERRIDES, config_root)
    yield Runs(default=Run(dirs["default"]), rollout=Run(dirs["rollout"]))


def test_by_default_the_file_is_byte_identical_to_the_one_written_before_m137(runs: Runs) -> None:
    text = runs.default.text()
    assert '"rollout"' not in text
    assert without_timestamps(text) == without_timestamps(GOLDEN.read_text(encoding="utf-8"))


def test_with_the_setting_on_the_file_carries_the_verdict_on_the_same_paired_difference(runs: Runs) -> None:
    switched = json.loads(runs.rollout.text())
    rollout = switched.pop("rollout")
    assert without_timestamps(json.dumps(switched)) == without_timestamps(
        json.dumps(json.loads(runs.default.text()))
    ), "the verdict adds one key and changes nothing else the comparison reports"
    report = runs.rollout.report()
    verdict = report.rollout
    assert verdict is not None and rollout["outcome"] == verdict.outcome
    assert verdict.confidence == 0.95
    difference = report.difference
    assert difference.ci_low is not None and difference.ci_high is not None
    # The same n·mean and n·sd as M96's two-sided interval; only the normal quantile differs.
    half_two_sided = (difference.ci_high - difference.ci_low) / 2.0
    ratio = NormalDist().inv_cdf(0.95) / NormalDist().inv_cdf(0.975)
    assert verdict.lower_bound_conversions == pytest.approx(difference.value - half_two_sided * ratio)
    assert verdict.upper_bound_conversions == pytest.approx(difference.value + half_two_sided * ratio)
    expected = (
        "safe"
        if verdict.lower_bound_conversions > 0
        else "not_safe" if verdict.upper_bound_conversions < 0 else "cannot_tell"
    )
    assert verdict.outcome == expected
    # M97's value inputs are set and the file carries the column: rupees, on M97's own basis.
    assert verdict.value_basis == "net_inr"
    assert verdict.rows_without_value == len(MISSING_VALUE_ROWS)
    assert verdict.lower_bound_inr is not None and verdict.upper_bound_inr is not None
    assert verdict.lower_bound_inr < verdict.upper_bound_inr
    assert verdict.inr_unavailable_reason is None
    if verdict.outcome == "cannot_tell" and difference.value > 0:
        assert verdict.rows_needed is not None and verdict.rows_needed > report.rows
    else:
        assert verdict.rows_needed is None


def test_the_sentence_is_on_the_approvers_check_beside_the_comparisons(runs: Runs) -> None:
    from engine.model_gates import approval_checks
    from engine.pilot.plain import jargon_in

    report = runs.rollout.report()
    assert report.rollout is not None
    check = next(
        c
        for c in approval_checks(runs.rollout.storage, runs.rollout.version())
        if c.code == UPLIFT_NOT_BETTER_THAN_RISK
    )
    assert check.message.endswith(f"{report.summary} {report.rollout.summary}")
    assert jargon_in(report.rollout.summary) == ()
    plain = next(
        c
        for c in approval_checks(runs.default.storage, runs.default.version())
        if c.code == UPLIFT_NOT_BETTER_THAN_RISK
    )
    assert plain.message.endswith(
        runs.default.report().summary
    ), "a run without the setting shows the comparison's sentence alone, as before"


def _update_golden() -> None:  # pragma: no cover - run by hand, on the code before M137
    import tempfile

    root = Path(__file__).resolve().parents[3] / "configs"
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        data_dir.mkdir()
        train(data_dir, M96_OVERRIDES, root)
        GOLDEN.write_bytes(LocalStorage(data_dir).read_bytes(run_key(TRAIN_RUN, RISK_COMPARISON)))


if __name__ == "__main__" and "--update" in sys.argv:  # pragma: no cover
    _update_golden()
