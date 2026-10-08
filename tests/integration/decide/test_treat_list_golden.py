"""Golden treat lists (Plan J M98 acceptance): propensity and uplift, with and without M97's value.

Each run is written by the product's own code (`tests/fixtures/decide/treat_runs.py`: Phase 1's
`apply_actions`, M97's `apply_uplift_actions`, the explain and export stages, M92's `assignment_frame`,
M97's `expected_gross_value_report`) and its treat list built by `build_treat_list`. The golden CSVs are
checked in; `python -m tests.fixtures.decide.treat_runs --update` rewrites them and the diff is the review.

Beside the byte comparison, every golden is checked against the artefacts it came from, not against
itself: holdout, explore and treat against `holdout_assignment.parquet` joined on the key (the engaged runs
write it in another row order), net value against the scores' `net_value`, expected gross value against
M97's own arithmetic and `expected_gross_value.json`, and the invariants of a treat list.
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from engine.decide.treat_list import (
    TREAT_LIST_CSV,
    TREAT_LIST_PARQUET,
    TREAT_LIST_SUMMARY_FILENAME,
    TreatListSummary,
    build_treat_list,
)
from engine.decide.value import EXPECTED_GROSS_VALUE_FILENAME, ExpectedGrossValue, expected_gross_values
from engine.storage import LocalStorage, run_key
from tests.fixtures.decide.treat_runs import (
    COSTS,
    GOLDEN_DIR,
    GOLDEN_RUNS,
    VALUE_COLUMN,
    WrittenRun,
    write_run,
)

pytestmark = pytest.mark.integration

NAMES = list(GOLDEN_RUNS)


def _build(name: str, tmp_path: Path) -> tuple[LocalStorage, WrittenRun, TreatListSummary]:
    storage = LocalStorage(tmp_path)
    spec = dict(GOLDEN_RUNS[name])
    run_id = spec.pop("run_id")
    written = write_run(storage, run_id, **spec)
    return storage, written, build_treat_list(storage, run_id)


def _csv(storage: LocalStorage, run_id: str) -> pd.DataFrame:
    text = storage.read_bytes(run_key(run_id, TREAT_LIST_CSV)).decode("utf-8")
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)


@pytest.mark.parametrize("name", NAMES)
def test_the_treat_list_is_the_checked_in_golden(name: str, tmp_path: Path) -> None:
    storage, written, _ = _build(name, tmp_path)
    produced = storage.read_bytes(run_key(written.run_id, TREAT_LIST_CSV))
    assert produced == (GOLDEN_DIR / f"{name}.csv").read_bytes()


@pytest.mark.parametrize("name", NAMES)
def test_the_golden_rows_follow_from_the_artefacts_they_came_from(name: str, tmp_path: Path) -> None:
    storage, written, summary = _build(name, tmp_path)
    csv = _csv(storage, written.run_id).set_index("customer_id")
    scores = written.scores.set_index("customer_id")
    assert list(csv.index) == list(scores.index), "one row per scored row, in the scores' order"

    suppressed = scores["suppressed_reason"].notna()
    control = scores["control_group"].astype(bool)
    assert (csv["suppression_reason"] == scores["suppressed_reason"].fillna("")).all()
    treat = csv["treat"] == "1"
    assert not (treat & suppressed).any(), "a suppressed customer is never treated"
    assert not (treat & control).any(), "a control customer is never treated"
    if written.kind == "uplift":
        assert not (treat & (scores["segment"] == "sleeping_dog")).any(), "a sleeping dog is never treated"
        assert (csv["segment"] == scores["segment"]).all() and "band" not in csv.columns
    else:
        assert (csv["band"] == scores["band"]).all() and "segment" not in csv.columns

    assignment = written.assignment
    if assignment is None:
        assert (csv["holdout"] == "").all() and (csv["explore"] == "").all(), "no assignment: null, not 0"
        assert summary.holdout_rows is None and summary.holdout_note is not None
        # Without M92's file the selection is M92's own function over the scores.
        selected = scores["intended_treatment"] if written.kind == "uplift" else scores["band"] != "Low"
        assert (treat == (~suppressed & ~control & selected.astype(bool))).all()
    else:
        by_key = assignment.set_index("customer_id")
        assert len(by_key) == len(scores) and not by_key.index.equals(scores.index), "another order"
        aligned = by_key.reindex(scores.index)
        assert (csv["holdout"] == aligned["holdout_member"].map({True: "1", False: "0"})).all()
        assert (csv["explore"] == aligned["explore"].map({True: "1", False: "0"})).all()
        assert (treat == aligned["treated"]).all(), "treat is M92's own column, read by key"
        assert summary.holdout_rows == int(aligned["holdout_member"].sum())
        assert summary.explore_rows == int(aligned["explore"].sum())
        assert summary.holdout_note is None
    assert summary.treat_rows == int(treat.sum())
    assert summary.suppressed_rows == int(suppressed.sum()) and summary.total_rows == len(scores)


def test_propensity_without_value_has_no_money_and_says_why(tmp_path: Path) -> None:
    storage, written, summary = _build("propensity_default", tmp_path)
    csv = _csv(storage, written.run_id)
    assert (csv["net_value"] == "").all() and (csv["expected_gross_value"] == "").all()
    assert summary.net_value_total is None and summary.expected_gross_value_total is None
    assert summary.net_value_unit is None
    assert summary.net_value_note
    assert summary.expected_gross_value_note == "Expected gross value was not configured for this run."


def test_propensity_with_value_fills_gross_value_and_leaves_net_value_null(tmp_path: Path) -> None:
    storage, written, summary = _build("propensity_value_holdout", tmp_path)
    csv = _csv(storage, written.run_id).set_index("customer_id")
    assert (csv["net_value"] == "").all(), "gross value is not net value: net_value stays null"

    # Worked out again from the arithmetic M97 documents, on the values as uploaded.
    upload = pd.read_csv(
        io.BytesIO(storage.read_bytes(f"uploads/u_{written.run_id[-8:]}/source.csv")),
        dtype={"customer_id": str},
    ).set_index("customer_id")
    scores = written.scores.set_index("customer_id")
    expected = expected_gross_values(
        scores[written.config.actions.score_field].to_numpy(dtype=float),
        pd.to_numeric(upload.reindex(scores.index)[VALUE_COLUMN], errors="coerce").to_numpy(dtype=float),
        written.config,
        value_costs=COSTS,
    )
    got = pd.to_numeric(csv["expected_gross_value"].replace("", np.nan)).to_numpy(dtype=float)
    assert np.allclose(got, np.round(expected, 2), equal_nan=True)
    assert np.isnan(got).sum() >= 2, "a missing or non-numeric value stays null, never zero"

    # It adds up to what M97 wrote for the run: the contactable rows' total.
    report = storage.read_model(run_key(written.run_id, EXPECTED_GROSS_VALUE_FILENAME), ExpectedGrossValue)
    contactable = (scores["suppressed_reason"].isna() & ~scores["control_group"].astype(bool)).to_numpy()
    assert float(np.nansum(np.where(contactable, got, 0.0))) == pytest.approx(report.total, abs=0.15)

    treat = (csv["treat"] == "1").to_numpy()
    assert summary.expected_gross_value_total == pytest.approx(float(np.nansum(np.where(treat, got, 0.0))))
    assert summary.expected_gross_value_note and "not incremental" in summary.expected_gross_value_note
    assert summary.net_value_total is None and summary.net_value_unit is None


def test_uplift_with_value_reads_m97s_net_value_column(tmp_path: Path) -> None:
    storage, written, summary = _build("uplift_value_holdout", tmp_path)
    csv = _csv(storage, written.run_id).set_index("customer_id")
    scores = written.scores.set_index("customer_id")
    assert "net_value" in scores.columns, "M97 wrote it into the scores"
    got = pd.to_numeric(csv["net_value"].replace("", np.nan)).to_numpy(dtype=float)
    assert np.allclose(got, np.round(scores["net_value"].to_numpy(dtype=float), 2), equal_nan=True)
    assert (csv["expected_gross_value"] == "").all(), "an uplift run's money is net value, not gross"
    treat = (csv["treat"] == "1").to_numpy()
    assert summary.net_value_total == pytest.approx(float(np.nansum(np.where(treat, got, 0.0))))
    assert summary.net_value_unit == "rupees" and summary.net_value_note is None


def test_uplift_without_value_has_null_net_value_and_a_note(tmp_path: Path) -> None:
    storage, written, summary = _build("uplift_default", tmp_path)
    csv = _csv(storage, written.run_id)
    assert "net_value" not in written.scores.columns
    assert (csv["net_value"] == "").all() and summary.net_value_total is None
    assert summary.net_value_note == "Net value was not configured for this run."


@pytest.mark.parametrize("name", NAMES)
def test_parquet_holds_typed_columns_and_the_csv_holds_one_and_zero(name: str, tmp_path: Path) -> None:
    storage, written, _ = _build(name, tmp_path)
    frame = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(written.run_id, TREAT_LIST_PARQUET))))
    assert frame["treat"].dtype == bool
    assert set(frame["holdout"].dropna().unique()) <= {True, False}
    assert frame["net_value"].dtype == "float64" and frame["expected_gross_value"].dtype == "float64"
    csv = _csv(storage, written.run_id)
    assert set(csv["treat"]) <= {"0", "1"} and set(csv["holdout"]) <= {"", "0", "1"}
    assert storage.exists(run_key(written.run_id, TREAT_LIST_SUMMARY_FILENAME))
