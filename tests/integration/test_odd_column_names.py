"""Plan A M36 item 2, end to end: a file with odd headers trains and scores under the client's names.

The library's credit-default sample with its target renamed back to the published
`default.payment.next.month`, and eight feature headers of the kinds a client's export really
carries - a comma, a quote, a slash, colons and braces, brackets, an accent, a dot, a header with no
ASCII in it at all - is trained with the real AutoGluon on the library's own, unchanged
`card-default-propensity` use case, then a later slice of it is scored. The tests read both runs:

* the model directory holds the stored mapping, and the predictor itself only ever saw safe names -
  before M36 LightGBM and XGBoost refused these headers and AutoGluon silently skipped both families
  ("No model was trained during hyperparameter tuning ... Skipping this model"). Which families a
  one-minute search completes also depends on the machine, so the refusal itself is pinned
  deterministically in `tests/unit/test_column_names.py` rather than read off this leaderboard;
* every artefact a person reads - the validation report, `prepare.json`, `schema.json`, the
  importance chart, the reasons, `scores.csv` - names each column exactly as the uploaded file did.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest

from engine.column_names import COLUMN_NAMES_FILENAME, ColumnNames, is_safe_name
from engine.config import ResolvedConfig, RunMode, resolve_config
from engine.contracts import (
    FeatureImportance,
    FeatureSchema,
    Leaderboard,
    PrepareReport,
    RunRecord,
    RunState,
    load_artefact,
)
from engine.jobs import CancelToken
from engine.pipeline import Pipeline, StageContext
from engine.registry import LocalModelRegistry
from engine.stages import explain
from engine.storage import LocalStorage, run_key, upload_key
from engine.utils.ids import new_run_id

pytestmark = [pytest.mark.slow, pytest.mark.integration]

REPO = Path(__file__).resolve().parents[2]
LIBRARY = REPO / "library"
# The library\'s use cases ship in the repository\'s configs/ since Plan A M38 (DEC-085).
USE_CASE = "card-default-propensity"
TARGET = "default.payment.next.month"
KEY = "ID"
ODD_HEADERS: dict[str, str] = {
    "default_payment_next_month": TARGET,
    "LIMIT_BAL": "limit, bal",
    "SEX": 'sex"q',
    "EDUCATION": "edu/level",
    "MARRIAGE": "婚姻",
    "PAY_2": "pay:2{x}",
    "AGE": "âge",
    "PAY_0": "PAY.0",
    "BILL_AMT1": "bill[amt]1",
}
OVERRIDES: dict[str, object] = {
    "model_search.time_limit_minutes": 1,
    "model_search.strategy": "fast",
    "model_search.tuning_trials": 5,
}


class _NoJobs:
    def submit(self, job_id: str, fn: object) -> object:  # pragma: no cover - never called
        raise AssertionError("the flows run on this thread")

    def status(self, job_id: str) -> object:  # pragma: no cover - never called
        raise AssertionError("the flows run on this thread")

    def cancel(self, job_id: str) -> bool:  # pragma: no cover - never called
        return False

    def shutdown(self, *, wait: bool = True) -> None:  # pragma: no cover - never called
        return None


@dataclass(frozen=True)
class Runs:
    storage: LocalStorage
    train: RunRecord
    score: RunRecord
    headers: tuple[str, ...]


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
        primary_key=KEY,
        target=TARGET if mode is RunMode.TRAIN else None,
        upload_key=source,
        model_version_id=model_version_id,
    )


def _store(storage: LocalStorage, frame: pd.DataFrame, name: str) -> str:
    key = upload_key(name, "source.csv")
    storage.write_bytes(key, frame.to_csv(index=False).encode("utf-8"))
    return key


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> Runs:
    root = tmp_path_factory.mktemp("odd-names")
    storage = LocalStorage(root / "data")
    registry = LocalModelRegistry(root / "registry.db")
    resolved = resolve_config(USE_CASE, OVERRIDES, root=LIBRARY.parent / "configs")
    pipeline = Pipeline(storage, registry, _NoJobs())

    frame = pd.read_csv(LIBRARY / "uci-credit-default" / "sample.csv").rename(columns=ODD_HEADERS)
    training, scoring = frame.iloc[:4000], frame.iloc[4000:].drop(columns=[TARGET])
    train = pipeline.run_train(
        _context(
            storage,
            registry,
            resolved,
            mode=RunMode.TRAIN,
            source=_store(storage, training, "u_odd_train"),
            model_version_id=None,
        )
    )
    assert train.state is RunState.DONE, train.error
    registry.approve(train.model_version_id or "", by="tests")
    score = pipeline.run_score(
        _context(
            storage,
            registry,
            resolved,
            mode=RunMode.SCORE,
            source=_store(storage, scoring, "u_odd_score"),
            model_version_id=train.model_version_id,
        )
    )
    assert score.state is RunState.DONE, score.error
    return Runs(storage=storage, train=train, score=score, headers=tuple(str(name) for name in frame.columns))


def _artefact(runs: Runs, run_id: str, name: str) -> object:
    return load_artefact(name, runs.storage.read_text(run_key(run_id, name)))


def test_the_model_saw_only_safe_names_and_the_mapping_is_stored_with_it(runs: Runs) -> None:
    from autogluon.tabular import TabularPredictor

    model = run_key(runs.train.run_id, "model")
    names = ColumnNames.model_validate_json(runs.storage.read_text(f"{model}/{COLUMN_NAMES_FILENAME}"))
    assert set(names.renamed) == set(ODD_HEADERS.values())
    scorer = json.loads(runs.storage.read_text(f"{model}/scorer.json"))
    assert all(is_safe_name(column) for column in scorer["feature_columns"])
    assert scorer["target_column"] == "default_payment_next_month"
    predictor = TabularPredictor.load(str(runs.storage.local_path(model)), require_version_match=True)
    assert all(is_safe_name(column) for column in predictor.features())
    leaderboard = _artefact(runs, runs.train.run_id, "leaderboard.json")
    assert isinstance(leaderboard, Leaderboard) and leaderboard.entries


def test_every_readable_artefact_names_the_clients_own_headers(runs: Runs) -> None:
    headers = set(runs.headers)
    internal_only = {"limit_bal", "sex_q", "edu_level", "column", "pay_2_x", "age", "bill_amt_1"}

    prepare = _artefact(runs, runs.train.run_id, "prepare.json")
    assert isinstance(prepare, PrepareReport)
    assert set(prepare.feature_columns) <= headers
    assert "limit, bal" in prepare.feature_columns

    schema = _artefact(runs, runs.train.run_id, "schema.json")
    assert isinstance(schema, FeatureSchema)
    assert {column.name for column in schema.columns} <= headers

    importance = _artefact(runs, runs.train.run_id, "feature_importance.json")
    assert isinstance(importance, FeatureImportance)
    assert importance.items, "permutation importance was measured"
    assert {item.feature for item in importance.items} <= headers

    for run_id in (runs.train.run_id, runs.score.run_id):
        key = (
            runs.train.artefacts.get(explain.ROW_EXPLANATIONS_FILENAME)
            if run_id == runs.train.run_id
            else None
        )
        explanations = explain.read_row_explanations(
            key or run_key(run_id, explain.ROW_EXPLANATIONS_FILENAME), storage=runs.storage
        )
        features = {reason.feature for explanation in explanations for reason in explanation.reasons}
        assert features <= headers
        assert not features & internal_only

    serialised = " ".join(
        runs.storage.read_text(run_key(runs.train.run_id, name))
        for name in ("prepare.json", "schema.json", "feature_importance.json", "validation.json")
    )
    for name in internal_only - headers:
        assert f'"{name}"' not in serialised


def test_scores_csv_is_keyed_and_explained_under_the_clients_headers(runs: Runs) -> None:
    text = runs.storage.read_text(run_key(runs.score.run_id, "scores.csv"))
    scores = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    assert scores.columns[0] == KEY
    assert len(scores.index) == 1000
    assert (scores["reason_1"] != "").all(), "every row has a reason"
    starts = tuple(runs.headers)
    assert scores["reason_1"].map(lambda reason: reason.startswith(starts)).all()
