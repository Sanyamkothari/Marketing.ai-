"""Ruling D4: the one statement Phase 4a added to the frozen train stage, held to its condition.

`engine/stages/train.py` calls `publish_local_path(storage, predictor_key)` immediately after
`predictor.save()` (DEC-312). Plan A approved that line on one condition: a test must prove that a
local run is unchanged by it. These tests are that proof, and its counterpart:

* on `LocalStorage` the call uploads nothing, writes nothing and moves nothing - every file under
  the store's root has the same bytes, size and modification time after it as before it, and no
  write method of the store is entered while it runs;
* on a store whose `local_path` is a mirror, the same call is what publishes the predictor, once,
  with the saved tree complete and before the scorer is fitted. That is the half the S3 deployment
  depends on - `S3ModelRegistry.register` copies the predictor out of the bucket, and nothing else
  uploads it before the register stage reads it - so removing the line has to fail a test here
  rather than surface as a registered model whose directory holds only `scorer.json`.

Each test runs the installed AutoGluon for real on a few hundred rows, one family and the minimum
trial count, which takes a few seconds: a monkeypatched `fit` would never call `save()` and so could
not show what the line after it does.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.config import EvaluationConfig, ModelFamily
from engine.jobs import CancelToken
from engine.stages import train as train_module
from engine.stages.scorer import SCORER_FILENAME
from engine.stages.train import predictor_key_for, train
from engine.storage import LocalStorage, SupportsLocalMirror, publish_local_path
from tests.unit.test_train import RUN_ID, make_parts, make_recipe

PREDICTOR_FILE = "predictor.pkl"
"""The predictor's own file, which `TabularPredictor.save()` writes; present once it has been saved."""

WRITE_METHODS = ("write_bytes", "write_text", "write_model", "open_write", "delete")
"""Every `Storage` member that can change what is stored."""


def quick_recipe() -> Any:
    """One cheap family, the fewest trials the schema allows and no bagging: seconds, not minutes."""
    return make_recipe(
        ensemble=False,
        candidates=[ModelFamily.LOGISTIC_REGRESSION],
        tuning_trials=5,
        time_limit_minutes=1,
    )


def run_train(storage: LocalStorage) -> train_module.TrainResult:
    return train(
        quick_recipe(),
        make_parts(),
        EvaluationConfig(),
        run_id=RUN_ID,
        storage=storage,
        cancel=CancelToken(),
    )


def snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    """`relative path -> (size, mtime_ns, sha256)` for every file under `root`."""
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@dataclass
class PublishCall:
    """What the spy saw at the one publish the train stage makes."""

    key: str
    published: tuple[str, ...]
    before: dict[str, tuple[int, int, str]]
    after: dict[str, tuple[int, int, str]]
    writes_during: list[str]


def test_local_storage_is_not_a_mirror() -> None:
    """The premise of the ruling: `LocalStorage` does not declare the capability the line dispatches on."""
    assert not issubclass(LocalStorage, SupportsLocalMirror)


def test_on_local_storage_the_publish_after_save_changes_nothing(tmp_path, monkeypatch) -> None:
    storage = LocalStorage(tmp_path / "data")
    calls: list[PublishCall] = []
    writes: list[str] = []
    inside = False

    # Every write the store could make is recorded while the publish is running; outside that
    # window the train stage writes `scorer.json` as it always has, and that is not this line's.
    for name in WRITE_METHODS:
        original = getattr(storage, name)

        def recording(*args: Any, _name: str = name, _original: Any = original, **kwargs: Any) -> Any:
            if inside:
                writes.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(storage, name, recording)

    def spy(store: Any, key: str) -> tuple[str, ...]:
        nonlocal inside
        assert store is storage, "the train stage must publish through the store it wrote with"
        before = snapshot(storage.root)
        inside = True
        try:
            published = publish_local_path(store, key)
        finally:
            inside = False
        calls.append(PublishCall(key, published, before, snapshot(storage.root), list(writes)))
        return published

    monkeypatch.setattr(train_module, "publish_local_path", spy)
    result = run_train(storage)

    assert len(calls) == 1, "the train stage publishes exactly once"
    call = calls[0]
    assert call.key == result.predictor_key == predictor_key_for(RUN_ID)
    assert call.published == (), "nothing is uploaded from a local store"
    assert call.writes_during == [], "no write method of the store is entered"
    assert call.after == call.before, "every file keeps its bytes, size and modification time"
    model_files = {path for path in call.before if path.startswith(f"{result.predictor_key}/")}
    assert f"{result.predictor_key}/{PREDICTOR_FILE}" in model_files, "the call comes after save()"

    # And the run's artefacts are exactly what they were before Phase 4a: the saved predictor, as
    # the call found it, plus the scorer the stage writes after it - nothing added, nothing moved.
    final = snapshot(storage.root)
    assert set(final) == set(call.before) | {f"{result.predictor_key}/{SCORER_FILENAME}"}
    assert {path: final[path] for path in call.before} == call.before


class RecordingMirror(LocalStorage):
    """A `LocalStorage` that claims its `local_path` is a mirror, and records what it is asked to publish.

    It stands in for `S3Storage` in the one respect that matters here - the dispatcher treats it as
    a mirror - without a bucket, so the test is about the train stage and not about S3.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.published: list[tuple[str, frozenset[str]]] = []

    def publish_local_path(self, key: str) -> tuple[str, ...]:
        directory = self.local_path(key)
        present = frozenset(
            path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()
        )
        self.published.append((key, present))
        return tuple(sorted(f"{key}/{name}" for name in present))

    def flush_local(self, prefix: str = "") -> tuple[str, ...]:
        return ()

    def release_local(self, prefix: str = "") -> None:
        return None


def test_on_a_mirrored_store_the_saved_predictor_is_published_once_before_the_scorer(tmp_path) -> None:
    storage = RecordingMirror(tmp_path / "data")
    assert isinstance(storage, SupportsLocalMirror)

    result = run_train(storage)

    assert len(storage.published) == 1, "the predictor is published exactly once"
    key, present = storage.published[0]
    assert key == result.predictor_key
    assert PREDICTOR_FILE in present, "published after save(), so the tree is complete"
    assert SCORER_FILENAME not in present, "published before the scorer fit, which can still fail"
    # Everything the stage saved through `local_path` was in the tree that was published: the only
    # file the model directory gained afterwards is the scorer, which is written through the store.
    directory = storage.local_path(result.predictor_key)
    after = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
    assert after == set(present) | {SCORER_FILENAME}
