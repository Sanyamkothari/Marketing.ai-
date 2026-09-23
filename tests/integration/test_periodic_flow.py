"""Plan A M34, end to end: a periodic dataset trains and scores on its two-column key (DEC-083).

One module-scoped fixture trains on customers observed at three month-ends - one row per customer
per snapshot date, keyed by `(entity_key, snapshot_date)` - with the real AutoGluon, approves the
model, and scores two later month-ends. The tests read those two runs:

* the model never learns from either key column, nor from the joined row key;
* no customer is in two of train, validation and test;
* every scored row comes back in `scores.csv` under the client's own ids - leading zeros intact, the
  date as the date - with a band, an action and a reason;
* a customer is in the control group at every snapshot of the scoring run or at none.

The ids are zero-padded on purpose: `000042` read back as `42` is a key that joins to nothing.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest

from engine import keys
from engine.config import ResolvedConfig, RunMode, resolve_config
from engine.contracts import Recipe, RunManifest, RunRecord, RunState, SplitReport, load_artefact
from engine.jobs import CancelToken
from engine.pipeline import MANIFEST_FILENAME, Pipeline, StageContext
from engine.registry import LocalModelRegistry
from engine.stages import explain
from engine.storage import LocalStorage, run_key, upload_key
from engine.utils.ids import new_run_id
from tests.fixtures.make_data import GenerationSpec, generate

pytestmark = [pytest.mark.slow, pytest.mark.integration]

USE_CASE = "targeted-advertisement"
TARGET = "converted_30d"
KEY = ["entity_key", "snapshot_date"]
CUSTOMERS = 3000
TRAIN_DATES = ("2026-01-31", "2026-02-28", "2026-03-31")
SCORE_DATES = ("2026-04-30", "2026-05-31")
OVERRIDES: dict[str, object] = {
    "model_search.time_limit_minutes": 1,
    "model_search.strategy": "fast",
    "model_search.tuning_trials": 5,
}


def periodic_frame(dates: tuple[str, ...], *, seed: int, target: bool) -> pd.DataFrame:
    """The same `CUSTOMERS` customers at each of `dates`, each snapshot drawn independently."""
    parts = []
    for offset, day in enumerate(dates):
        frame = generate(GenerationSpec(USE_CASE, rows=CUSTOMERS, variant="clean", seed=seed + offset))
        frame = frame.drop(columns=["customer_id", "snapshot_date"])
        frame.insert(0, "snapshot_date", pd.Timestamp(day))
        frame.insert(0, "entity_key", [f"{i:06d}" for i in range(CUSTOMERS)])
        parts.append(frame)
    result = pd.concat(parts, ignore_index=True)
    return result if target else result.drop(columns=[TARGET])


def store(storage: LocalStorage, frame: pd.DataFrame, name: str) -> str:
    """Written as Parquet, as a built dataset is, so the ids stay text."""
    key = upload_key(name, "source.parquet")
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    storage.write_bytes(key, buffer.getvalue())
    return key


@dataclass(frozen=True)
class Runs:
    storage: LocalStorage
    train: RunRecord
    score: RunRecord
    scored_input: pd.DataFrame
    resolved: ResolvedConfig


def _context(
    storage: LocalStorage,
    registry: LocalModelRegistry,
    resolved: ResolvedConfig,
    *,
    mode: RunMode,
    source: str,
    model_version_id: str | None,
) -> StageContext:
    run_id = new_run_id()
    storage.write_model(run_key(run_id, "run_config.json"), resolved)
    return StageContext(
        run_id=run_id,
        mode=mode,
        config=resolved.config,
        resolved=resolved,
        storage=storage,
        registry=registry,
        cancel=CancelToken(),
        primary_key=list(KEY),
        target=TARGET if mode is RunMode.TRAIN else None,
        upload_key=source,
        model_version_id=model_version_id,
    )


class _NoJobs:
    def submit(self, job_id: str, fn: object) -> object:  # pragma: no cover - never called
        raise AssertionError("the flows run on this thread")

    def status(self, job_id: str) -> object:  # pragma: no cover - never called
        raise AssertionError("the flows run on this thread")

    def cancel(self, job_id: str) -> bool:  # pragma: no cover - never called
        return False

    def shutdown(self, *, wait: bool = True) -> None:  # pragma: no cover - never called
        return None


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory, config_root: Path) -> Runs:
    root = tmp_path_factory.mktemp("periodic-flow")
    storage = LocalStorage(root / "data")
    registry = LocalModelRegistry(root / "registry.db")
    resolved = keys.split_config_for_key(resolve_config(USE_CASE, OVERRIDES, root=config_root), KEY)
    pipeline = Pipeline(storage, registry, _NoJobs())

    training = periodic_frame(TRAIN_DATES, seed=20260923, target=True)
    train = pipeline.run_train(
        _context(
            storage,
            registry,
            resolved,
            mode=RunMode.TRAIN,
            source=store(storage, training, "u_periodic_train"),
            model_version_id=None,
        )
    )
    assert train.state is RunState.DONE, train.error
    registry.approve(train.model_version_id or "", by="tests")

    scoring = periodic_frame(SCORE_DATES, seed=20261001, target=False)
    score = pipeline.run_score(
        _context(
            storage,
            registry,
            resolved,
            mode=RunMode.SCORE,
            source=store(storage, scoring, "u_periodic_score"),
            model_version_id=train.model_version_id,
        )
    )
    assert score.state is RunState.DONE, score.error
    return Runs(storage=storage, train=train, score=score, scored_input=scoring, resolved=resolved)


def _artefact(runs: Runs, run_id: str, name: str) -> object:
    return load_artefact(name, runs.storage.read_text(run_key(run_id, name)))


def _scores(runs: Runs) -> pd.DataFrame:
    text = runs.storage.read_text(run_key(runs.score.run_id, "scores.csv"))
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)


def test_the_run_records_both_key_columns(runs: Runs) -> None:
    assert runs.train.primary_key == KEY
    manifest = _artefact(runs, runs.train.run_id, MANIFEST_FILENAME)
    assert isinstance(manifest, RunManifest)
    assert manifest.primary_key == KEY


def test_no_key_column_is_ever_a_feature(runs: Runs) -> None:
    manifest = _artefact(runs, runs.train.run_id, MANIFEST_FILENAME)
    assert isinstance(manifest, RunManifest)
    recipe = manifest.recipe
    assert isinstance(recipe, Recipe)
    assert not {*KEY, keys.ROW_KEY_COLUMN} & set(recipe.feature_columns)


def test_the_split_keeps_every_customer_in_one_part(runs: Runs) -> None:
    split = _artefact(runs, runs.train.run_id, "split.json")
    assert isinstance(split, SplitReport)
    assert split.group_column == "entity_key"
    explanations = explain.read_row_explanations(
        run_key(runs.train.run_id, explain.ROW_EXPLANATIONS_FILENAME), storage=runs.storage
    )
    # The test part is explained row by row, keyed by the joined row key: every customer in it
    # appears at every training snapshot, because none of their rows went to another part.
    per_customer: dict[str, set[str]] = {}
    for item in explanations:
        entity, _, day = item.primary_key.partition(keys.KEY_SEPARATOR)
        per_customer.setdefault(entity, set()).add(day)
    assert per_customer
    assert all(days == set(TRAIN_DATES) for days in per_customer.values())


def test_scores_csv_has_one_row_per_uploaded_row_under_the_clients_own_ids(runs: Runs) -> None:
    scores = _scores(runs)
    assert list(scores.columns[:2]) == KEY
    assert keys.ROW_KEY_COLUMN not in scores.columns
    assert len(scores.index) == len(runs.scored_input.index)
    got = set(zip(scores["entity_key"], scores["snapshot_date"], strict=True))
    expected = {
        (entity, day.strftime("%Y-%m-%d"))
        for entity, day in zip(
            runs.scored_input["entity_key"], runs.scored_input["snapshot_date"], strict=True
        )
    }
    assert got == expected
    assert "000042" in set(scores["entity_key"])


def test_every_scored_row_has_a_band_an_action_and_a_reason(runs: Runs) -> None:
    scores = _scores(runs)
    assert (scores["band"] != "").all()
    assert (scores["action"] != "").all()
    assert (scores["reason_1"] != "").all()


def test_a_customer_is_control_at_every_snapshot_or_at_none(runs: Runs) -> None:
    scores = _scores(runs)
    per_customer = scores.groupby("entity_key")["control_group"].nunique()
    assert (per_customer == 1).all()
    assert (scores["control_group"] == "True").any()
