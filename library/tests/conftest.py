"""Shared fixtures for the library's dataset tests.

These tests are **not** in pytest's default `testpaths` (`pyproject.toml` sets it to `tests`), so
`make test` does not pick them up. Run them on purpose:

    python -m pytest library/tests            # all six, about four minutes
    python -m pytest library/tests -k telco

Each one trains a real model on a committed `sample.csv` with `strategy: fast` and
`time_limit_minutes: 1`, which is the same budget plan section 10's integration run uses.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from library.run_engine import run  # noqa: E402

LIBRARY = REPO_ROOT / "library"
LIBRARY_CONFIGS = LIBRARY / "configs"

#: How far below its own logistic-regression baseline a one-minute search is allowed to land.
#: Not a fudge factor, and not a number chosen to make a test pass: across the four datasets that
#: use it, the largest shortfall ever measured is 0.0035 ROC-AUC, so this is roughly six times the
#: observed noise. `online-retail` does not use it — its worst run lands 0.0484 below, which is why
#: that dataset asserts no score at all. DEC-410 records every run, and each test's docstring
#: repeats the ones for its own dataset.
BASELINE_TOLERANCE: float = 0.02

#: plan section 10's budget: the smallest search that still produces every artefact.
FAST: dict[str, Any] = {
    "model_search.time_limit_minutes": 1,
    "model_search.strategy": "fast",
}


@dataclass(frozen=True)
class SampleRun:
    """One finished run over a committed sample, with the numbers a test asserts on."""

    results: dict[str, Any]

    @property
    def state(self) -> str:
        return str(self.results["state"])

    @property
    def validation(self) -> dict[str, Any]:
        return dict(self.results["validation"])

    @property
    def codes(self) -> list[str]:
        return [check["code"] for check in self.validation["checks"]]

    def metric(self, name: str) -> float:
        return float(self.results["evaluation"]["metrics"][name])

    def baseline(self, name: str) -> float:
        for row in self.results["baseline"]["rows"]:
            if row["metric"] == name:
                return float(row["baseline_value"])
        raise KeyError(name)

    @property
    def beats_baseline(self) -> bool:
        return bool(self.results["baseline"]["model_beats_baseline"])

    @property
    def decile_one_lift(self) -> float:
        return float(self.results["decile_lift"]["bins"][0]["lift"])


def train_on_sample(
    *,
    dataset: str,
    use_case: str,
    sample: str,
    primary_key: str,
    target: str,
    config_root: Path | None = LIBRARY_CONFIGS,
) -> SampleRun:
    """Validate and train on `library/<sample>`, then hand back the numbers.

    The run is refused rather than crashed when validation fails, so a test can assert on the
    findings either way; `test_the_run_finished` is what turns a refusal into a failure.
    """
    csv_path = LIBRARY / sample
    if not csv_path.is_file():
        pytest.skip(f"{csv_path} is missing; run the dataset's fetch.py to rebuild it")
    outcome = run(
        dataset=f"test-{dataset}",
        use_case=use_case,
        csv_path=csv_path,
        primary_key=primary_key,
        target=target,
        overrides=dict(FAST),
        config_root=config_root,
    )
    return SampleRun(results=outcome.results)


def assert_trained_and_validated(sample_run: SampleRun) -> None:
    """The three things every dataset in the library must do, whatever its scores turn out to be."""
    assert sample_run.state == "done", f"the run did not finish: {sample_run.results['error']}"
    assert sample_run.validation["error_count"] == 0, sample_run.validation["checks"]
    assert sample_run.validation["passed"] is True
