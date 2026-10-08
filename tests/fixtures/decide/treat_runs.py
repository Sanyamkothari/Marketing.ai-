"""Small, deterministic scoring runs written into a store, for the treat list tests (Plan J M98).

Every artefact comes from the product's own code, not from a hand-made frame of the result: Phase 1's
`apply_actions` (propensity) or M97's `apply_uplift_actions` (uplift) decide band, action, suppression and
control group; `engine.stages.explain` joins the reasons; `engine.stages.export` and the uplift flow's
writer write `scores.*`; M92's `assignment_frame` writes `holdout_assignment.parquet`; M97's
`expected_gross_value_report` writes `expected_gross_value.json`. The customers are fixed by index, not
drawn, so the same call writes the same run on any machine, and the goldens under `golden/` are the
treat lists the builder must produce from them.

`python -m tests.fixtures.decide.treat_runs --update` rewrites the goldens; the diff is then the review.
"""

from __future__ import annotations

import argparse
import io
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

import numpy as np
import pandas as pd

from engine import __version__
from engine.config import ProblemType, ResolvedConfig, RunMode, UseCaseConfig, resolve_config
from engine.contracts import Direction, Reason, RowExplanation, RunRecord, RunState
from engine.decide.treat_list import TREAT_LIST_CSV, build_treat_list
from engine.decide.value import EXPECTED_GROSS_VALUE_FILENAME, expected_gross_value_report
from engine.holdout.assign import (
    HOLDOUT_ASSIGNMENT_FILENAME,
    ActiveHoldout,
    assignment_frame,
    holdout_context,
)
from engine.keys import ROW_KEY_COLUMN, with_row_key
from engine.pilot.roi import ValueCosts
from engine.runs import RUN_CONFIG_FILENAME, RUN_FILENAME
from engine.stages import explain, export
from engine.stages.actions import apply_actions
from engine.storage import LocalStorage, Storage, run_key, upload_key
from engine.uplift import flow as uplift_flow
from engine.uplift.actions import apply_uplift_actions
from engine.uplift.contracts import SegmentThresholds

__all__ = [
    "COSTS",
    "GOLDEN_DIR",
    "GOLDEN_RUNS",
    "SALT",
    "USE_CASE",
    "VALUE_COLUMN",
    "WrittenRun",
    "run_config",
    "write_run",
]

USE_CASE: Final[str] = "win-back-campaign"
VALUE_COLUMN: Final[str] = "avg_monthly_spend"
SALT: Final[str] = "treat-list-test-salt-0001"
HOLDOUT_FRACTION: Final[float] = 0.25
EXPLORE_FRACTION: Final[float] = 0.5
FINISHED: Final[datetime] = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
COSTS: Final[ValueCosts] = ValueCosts(offer_cost=2.0, contact_cost=1.0)
GOLDEN_DIR: Final[Path] = Path(__file__).with_name("golden")
THRESHOLDS: Final[SegmentThresholds] = SegmentThresholds(
    persuadable_min_uplift=0.02,
    sleeping_dog_max_uplift=-0.01,
    sure_thing_min_probability=0.40,
    sure_thing_from_base_rate=False,
)

Kind = Literal["propensity", "uplift"]

_FEATURES: Final[tuple[str, ...]] = (
    "monthly_spend",
    "tenure_months",
    "complaints_30d",
    "months_since_churn",  # not in reasons.yaml: keeps the model's own text
    "visits_30d",
)


@dataclass(frozen=True)
class WrittenRun:
    """What `write_run` wrote, for a test to compare the treat list against."""

    run_id: str
    kind: Kind
    scores: pd.DataFrame
    assignment: pd.DataFrame | None
    explanations: tuple[RowExplanation, ...]
    config: UseCaseConfig


def run_config(overrides: dict[str, Any] | None = None) -> ResolvedConfig:
    """The win-back use case resolved as a run would: `run_config.json`."""
    return resolve_config(USE_CASE, overrides or {}, now=FINISHED)


def _customers(rows: int, *, composite: bool) -> pd.DataFrame:
    """Fixed customers: a score spread over all three bands, some opted out, some without a value."""
    index = np.arange(rows)
    customers = rows // 2 if composite else rows
    frame = pd.DataFrame(
        {
            "customer_id": [f"C-{(i % customers):03d}" for i in index],
            "winback_prob": np.round(((index * 37) % 100) / 100.0, 2),
            "marketing_opt_in": (index % 7) != 5,
            VALUE_COLUMN: np.round(500.0 + 25.0 * ((index * 11) % 40), 1),
        }
    )
    if composite:
        frame["snapshot_date"] = ["2026-08-01" if i < customers else "2026-09-01" for i in index]
    frame[VALUE_COLUMN] = frame[VALUE_COLUMN].astype("object")
    frame.loc[frame.index % 9 == 4, VALUE_COLUMN] = None  # a missing value stays null in the treat list
    frame.loc[frame.index % 13 == 7, VALUE_COLUMN] = "n/a"  # so does one that is not a number
    return frame


def _explanations(row_keys: list[str]) -> tuple[RowExplanation, ...]:
    """Three reasons per row from a fixed pattern: both directions, a general one, one unmapped feature.

    Rows 3 mod 8 have one reason and 5 mod 8 have none, so the empty slots are null in the treat list.
    """
    out: list[RowExplanation] = []
    for i, key in enumerate(row_keys):
        count = 1 if i % 8 == 3 else 0 if i % 8 == 5 else 3
        reasons = []
        for slot in range(count):
            feature = _FEATURES[(i + slot) % len(_FEATURES)]
            direction = (Direction.UP, Direction.DOWN, Direction.NONE)[(i + 2 * slot) % 3]
            value = str((i * 7 + slot * 3) % 50 + 1)
            contribution = (
                0.0 if direction is Direction.NONE else (0.2 if direction is Direction.UP else -0.2)
            )
            reasons.append(
                Reason(
                    feature=feature,
                    value=value,
                    contribution=contribution,
                    direction=direction,
                    text=f"{feature} = {value}",
                )
            )
        out.append(RowExplanation(primary_key=key, score=0.5, reasons=tuple(reasons)))
    return tuple(out)


def _record(run_id: str, *, kind: Kind, rows: int, primary_key: str | list[str], upload_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        use_case_id=USE_CASE,
        use_case_name="Win-back Campaign",
        mode=RunMode.SCORE,
        state=RunState.DONE,
        created_at=FINISHED - timedelta(minutes=5),
        started_at=FINISHED - timedelta(minutes=4),
        finished_at=FINISHED,
        upload_id=upload_id,
        file_name="winback_campaign.csv",
        row_count=rows,
        primary_key=primary_key,
        problem_type=ProblemType.UPLIFT if kind == "uplift" else ProblemType.BINARY_CLASSIFICATION,
        model_choice="auto",
        model_version_id=f"m_{USE_CASE}_1",
        best_model="LightGBM",
        engine_version=__version__,
    )


def write_run(
    storage: Storage,
    run_id: str,
    *,
    kind: Kind = "propensity",
    rows: int = 24,
    value: bool = False,
    holdout: bool = False,
    composite: bool = False,
    shuffle_assignment: bool = False,
    drop_from_assignment: int = 0,
    explanations: bool = True,
    shuffle_explanations: bool = False,
    drop_explanations: int = 0,
    config_overrides: dict[str, Any] | None = None,
    config: ResolvedConfig | None = None,
) -> WrittenRun:
    """Write a finished scoring run's artefacts: `run.json`, `run_config.json`, `scores.*` and friends.

    `value` sets `uplift.policy.value_column` (so an uplift run is ranked by M97's net value and a
    propensity run writes `expected_gross_value.json` and keeps its upload). `holdout` runs the actions
    stage under a persistent holdout and writes `holdout_assignment.parquet` (`shuffle_assignment` in
    another row order, `drop_from_assignment` rows missing). `composite` keys by customer and snapshot.
    `shuffle_explanations` and `drop_explanations` do the same to `row_explanations.parquet`.
    """
    if composite and kind == "uplift":
        raise ValueError("the composite-key fixture is a propensity run")
    overrides = dict(config_overrides or {})
    if value:
        overrides |= {"uplift.policy.value_column": VALUE_COLUMN, "uplift.policy.margin_pct": 30.0}
    resolved = config if config is not None else run_config(overrides)
    cfg = resolved.config
    primary_key: str | list[str] = ["customer_id", "snapshot_date"] if composite else "customer_id"
    row_key = ROW_KEY_COLUMN if composite else "customer_id"
    entity_key = "customer_id" if composite else None

    frame = _customers(rows, composite=composite)
    framed = with_row_key(frame, primary_key) if composite else frame
    active = ActiveHoldout(salt=SALT, scope_key="universal", fraction=HOLDOUT_FRACTION) if holdout else None
    reasons = _explanations(framed[row_key].astype(str).tolist())

    if kind == "propensity":
        with holdout_context(active):
            banded = apply_actions(
                framed, cfg, run_id=run_id, primary_key=row_key, entity_key=entity_key, now=FINISHED
            )
        scored = (
            explain.with_reason_columns(reasons, banded, cfg, primary_key=row_key) if explanations else banded
        )
        export.write_scores(
            scored, cfg, run_id=run_id, primary_key=primary_key, storage=storage, key_source=framed
        )
    else:
        index = np.arange(rows)
        uplift = np.round(((index * 53) % 41 - 14) / 100.0, 2)
        p_control = np.round(0.10 + ((index * 17) % 30) / 100.0, 2)
        framed = framed.assign(
            uplift=uplift, p_control=p_control, p_treated=np.clip(p_control + uplift, 0, 1)
        )
        framed["winback_prob"] = framed["p_treated"]
        values = pd.to_numeric(framed[VALUE_COLUMN], errors="coerce")
        framed[VALUE_COLUMN] = values
        with holdout_context(active):
            banded, _ = apply_uplift_actions(
                framed,
                cfg,
                run_id=run_id,
                primary_key=row_key,
                causal=True,
                thresholds=THRESHOLDS,
                now=FINISHED,
                value_costs=COSTS,
            )
        scored = (
            explain.with_reason_columns(reasons, banded, cfg, primary_key=row_key) if explanations else banded
        )
        uplift_flow._write_scores(scored, cfg, run_id=run_id, primary_key=primary_key, storage=storage)

    scores = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(run_id, export.SCORES_PARQUET))))
    if explanations:
        written = list(reasons[: len(reasons) - drop_explanations])
        if shuffle_explanations:
            written = [written[i] for i in np.random.default_rng(5).permutation(len(written))]
        explain.write_row_explanations(written, run_id=run_id, storage=storage)

    assignment: pd.DataFrame | None = None
    if holdout:
        assignment = assignment_frame(
            scored,
            cfg,
            primary_key=primary_key,
            row_key=row_key,
            entity_key=entity_key,
            run_id=run_id,
            active=active,
            explore_fraction=EXPLORE_FRACTION,
            key_source=framed if composite else None,
        ).table
        if drop_from_assignment:
            assignment = assignment.iloc[:-drop_from_assignment]
        if shuffle_assignment:
            assignment = assignment.iloc[np.random.default_rng(3).permutation(len(assignment))]
        buffer = io.BytesIO()
        assignment.to_parquet(buffer, index=False)
        storage.write_bytes(run_key(run_id, HOLDOUT_ASSIGNMENT_FILENAME), buffer.getvalue())

    upload_id = f"u_{run_id[-8:]}"
    if value and kind == "propensity":
        storage.write_bytes(
            upload_key(upload_id, "source.csv"),
            frame.to_csv(index=False, lineterminator="\n").encode("utf-8"),
        )
        report = expected_gross_value_report(
            scored, framed, cfg, run_id=run_id, row_key=row_key, value_costs=COSTS
        )
        assert report is not None, "the fixture opted in, so the report exists"
        storage.write_model(run_key(run_id, EXPECTED_GROSS_VALUE_FILENAME), report)

    storage.write_model(run_key(run_id, RUN_CONFIG_FILENAME), resolved)
    storage.write_model(
        run_key(run_id, RUN_FILENAME),
        _record(run_id, kind=kind, rows=rows, primary_key=primary_key, upload_id=upload_id),
    )
    return WrittenRun(run_id, kind, scores, assignment, reasons, cfg)


GOLDEN_RUNS: Final[dict[str, dict[str, Any]]] = {
    "propensity_default": {"run_id": "r_20261001_0a000001", "kind": "propensity"},
    "propensity_value_holdout": {
        "run_id": "r_20261001_0a000002",
        "kind": "propensity",
        "value": True,
        "holdout": True,
        "shuffle_assignment": True,
    },
    "uplift_default": {"run_id": "r_20261001_0a000003", "kind": "uplift"},
    "uplift_value_holdout": {
        "run_id": "r_20261001_0a000004",
        "kind": "uplift",
        "value": True,
        "holdout": True,
        "shuffle_assignment": True,
    },
}
"""The four golden runs: propensity and uplift, each without and with M97's value (and a persistent holdout)."""


def golden_csv(name: str, storage: Storage) -> bytes:
    """Write the golden run `name` into `storage`, build its treat list and return `treat_list.csv`."""
    spec = dict(GOLDEN_RUNS[name])
    run_id = spec.pop("run_id")
    write_run(storage, run_id, **spec)
    build_treat_list(storage, run_id)
    return storage.read_bytes(run_key(run_id, TREAT_LIST_CSV))


def main() -> None:
    import tempfile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="rewrite the golden CSVs")
    args = parser.parse_args()
    for name in GOLDEN_RUNS:
        with tempfile.TemporaryDirectory() as tmp:
            produced = golden_csv(name, LocalStorage(Path(tmp)))
        path = GOLDEN_DIR / f"{name}.csv"
        if args.update:
            GOLDEN_DIR.mkdir(exist_ok=True)
            path.write_bytes(produced)
        print(f"== {name}\n{produced.decode()}")


if __name__ == "__main__":
    main()
