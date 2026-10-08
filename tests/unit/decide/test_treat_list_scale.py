"""Plan J M98: the builder at scale.

`build_treat_list` is timed on 200,000 customers with everything it reads present: scores, a holdout
assignment in another row order, three reasons per customer in `row_explanations.parquet`, and M97's
expected gross value from the upload. The review found per-row `.apply` and list comprehensions in the
builder and the CSV writer, and a timing test of the reason mapping only; this times the builder.

The 1M-row figure in `docs/handoff/M98.md` is five times the 200k measurement (linear: every step is a
vectorised column operation or a hash join on the key).
"""

from __future__ import annotations

import io
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from engine.contracts import RowExplanation
from engine.decide.treat_list import TREAT_LIST_CSV, TREAT_LIST_PARQUET, build_treat_list
from engine.decide.value import EXPECTED_GROSS_VALUE_FILENAME, ExpectedGrossValue
from engine.holdout.assign import HOLDOUT_ASSIGNMENT_FILENAME
from engine.stages.explain import ROW_EXPLANATIONS_FILENAME, row_explanation_schema
from engine.stages.export import SCORES_PARQUET
from engine.storage import LocalStorage, run_key, upload_key
from tests.fixtures.decide.treat_runs import USE_CASE, VALUE_COLUMN, run_config
from tests.fixtures.decide.treat_runs import _record as run_record  # the fixture's run.json

ROWS = 200_000
FAST_ROWS = 20_000
BUDGET_SECONDS = 3.0
"""The review's line between a fast test and a slow one; the measurement is reported either way."""
FEATURES = np.array(["monthly_spend", "tenure_months", "complaints_30d", "months_since_churn", "visits_30d"])


def _write_scale_run(storage: LocalStorage, run_id: str, rows: int = ROWS) -> None:
    rng = np.random.default_rng(1)
    ids = np.array([f"C-{i:07d}" for i in range(rows)], dtype=object)
    score = np.round(rng.random(rows), 4)
    band = np.where(score >= 0.8, "High", np.where(score >= 0.5, "Medium", "Low")).astype(object)
    suppressed = np.where(rng.random(rows) < 0.05, "opted_out", None).astype(object)
    control = (rng.random(rows) < 0.10) & (suppressed == None)  # noqa: E711 - elementwise on an object array
    scores = pd.DataFrame(
        {
            "customer_id": ids,
            "winback_prob": score,
            "band": band,
            "action": np.where(band == "Low", "Hold", "Send offer").astype(object),
            "reason_1": "monthly_spend ↑ (5)",
            "reason_2": None,
            "reason_3": None,
            "suppressed_reason": suppressed,
            "control_group": control,
        }
    )
    buffer = io.BytesIO()
    scores.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(run_id, SCORES_PARQUET), buffer.getvalue())

    eligible = suppressed == None  # noqa: E711
    order = rng.permutation(rows)
    treated = eligible & ~control & (band != "Low")
    assignment = pd.DataFrame(
        {
            "customer_id": ids,
            "holdout_member": control,
            "explore": np.zeros(rows, dtype=bool),
            "explore_probability": np.zeros(rows),
            "treated": treated,
            "treatment_probability": np.where(treated, 0.9, 0.0),
        }
    ).iloc[order]
    buffer = io.BytesIO()
    assignment.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(run_id, HOLDOUT_ASSIGNMENT_FILENAME), buffer.getvalue())

    # Three reasons per customer, written as the explain stage's list-of-struct column, in another order.
    n = rows * 3
    feature = pa.array(FEATURES[rng.integers(0, len(FEATURES), size=n)], type=pa.string())
    direction = pa.array(np.array(["up", "down", "none"])[rng.integers(0, 3, size=n)], type=pa.string())
    value = pa.array(rng.integers(0, 5000, size=n).astype(str), type=pa.string())
    contribution = pa.array(rng.normal(size=n), type=pa.float64())
    text = pa.array(np.full(n, "monthly_spend ↑ (5)", dtype=object), type=pa.string())
    structs = pa.StructArray.from_arrays(
        [feature, value, contribution, direction, text],
        fields=list(row_explanation_schema().field("reasons").type.value_type),
    )
    reasons = pa.ListArray.from_arrays(pa.array(np.arange(0, n + 1, 3, dtype=np.int32)), structs)
    table = pa.table(
        {
            "schema_version": pa.array(np.ones(rows, dtype=np.int32), type=pa.int32()),
            "primary_key": pa.array(ids[order], type=pa.string()),
            "score": pa.array(score[order], type=pa.float64()),
            "method": pa.array(np.full(rows, "TreeSHAP", dtype=object), type=pa.string()),
            "reasons": reasons.take(pa.array(order)),
        },
        schema=row_explanation_schema(),
    )
    buffer = io.BytesIO()
    pq.write_table(table, buffer)  # type: ignore[no-untyped-call]
    storage.write_bytes(run_key(run_id, ROW_EXPLANATIONS_FILENAME), buffer.getvalue())

    # M97's expected gross value: the report and the uploaded values.
    upload_id = f"u_{run_id[-8:]}"
    upload = pd.DataFrame({"customer_id": ids, VALUE_COLUMN: np.round(rng.random(rows) * 3000, 1)})
    storage.write_bytes(
        upload_key(upload_id, "source.parquet"),
        upload.to_parquet(index=False),
    )
    report = ExpectedGrossValue(
        run_id=run_id,
        value_column=VALUE_COLUMN,
        score_field="winback_prob",
        contact_cost=1.0,
        offer_cost=2.0,
        margin_pct=30.0,
        horizon_months=None,
        rows=rows,
        contactable_rows=int(treated.sum()),
        rows_missing_value=0,
        total=0.0,
        by_band=(),
    )
    storage.write_model(run_key(run_id, EXPECTED_GROSS_VALUE_FILENAME), report)
    storage.write_model(
        run_key(run_id, "run_config.json"),
        run_config({"uplift.policy.value_column": VALUE_COLUMN, "uplift.policy.margin_pct": 30.0}),
    )
    storage.write_model(
        run_key(run_id, "run.json"),
        run_record(run_id, kind="propensity", rows=rows, primary_key="customer_id", upload_id=upload_id),
    )


def _timed_build(tmp_path: Path, rows: int, run_id: str) -> tuple[float, int]:
    storage = LocalStorage(tmp_path)
    _write_scale_run(storage, run_id, rows)
    started = time.perf_counter()
    summary = build_treat_list(storage, run_id)
    elapsed = time.perf_counter() - started
    assert summary.total_rows == rows and summary.treat_rows > 0.4 * rows
    csv = storage.read_bytes(run_key(run_id, TREAT_LIST_CSV))
    parquet = pq.read_table(io.BytesIO(storage.read_bytes(run_key(run_id, TREAT_LIST_PARQUET))))  # type: ignore[no-untyped-call]
    assert csv.count(b"\n") == rows + 1 and parquet.num_rows == rows
    assert summary.expected_gross_value_total is not None
    assert USE_CASE in csv[:400].decode()
    return elapsed, rows


def test_building_the_treat_list_for_20k_customers_is_fast(tmp_path: Path) -> None:
    elapsed, rows = _timed_build(tmp_path, FAST_ROWS, "r_20261001_0d000001")
    print(f"\n[Perf] build_treat_list, {rows:,} rows: {elapsed:.2f}s; per 1M {elapsed * 50:.1f}s")
    assert elapsed < BUDGET_SECONDS / 2, f"{rows:,} rows took {elapsed:.2f}s"


@pytest.mark.slow
def test_building_the_treat_list_for_200k_customers_is_linear(tmp_path: Path) -> None:
    """Slow because the 200k run's own fixture and the build take about 3 s: the review's line for `slow`."""
    # The best of three small builds: a single timing on a machine shared with other test workers can
    # be inflated several-fold by contention, which made the ratio below fail at random under -n 4.
    small = min(_timed_build(tmp_path / f"small{i}", FAST_ROWS, "r_20261001_0d000002")[0] for i in range(3))
    large, rows = _timed_build(tmp_path / "large", ROWS, "r_20261001_0d000003")
    print(
        f"\n[Perf] build_treat_list, {rows:,} rows (scores, shuffled assignment, 3 reasons each, gross value): "
        f"{large:.2f}s; 1M estimate {large * 5:.1f}s; {FAST_ROWS:,} rows {small:.2f}s"
    )
    assert large < 2 * BUDGET_SECONDS, f"{rows:,} rows took {large:.2f}s"
    # Linear work gives about 10x for ten times the rows and quadratic work about 100x; 30x leaves room
    # for contention while still catching anything worse than linear.
    assert large < 30 * max(small, 0.05), "ten times the rows took far more than ten times as long"


def test_the_fixture_explanation_schema_is_the_explain_stages() -> None:
    assert [f.name for f in row_explanation_schema()] == [
        "schema_version",
        "primary_key",
        "score",
        "method",
        "reasons",
    ]
    assert set(RowExplanation.model_fields) >= {"primary_key", "score", "reasons", "method"}
