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
not show what the line after it does. `TabularPredictor.save` itself is wrapped, not replaced, so the
order of save and publish is observed directly. The presence of `predictor.pkl` cannot show it:
`fit()` already writes that file before the stage calls `save()`.
"""

from __future__ import annotations

import hashlib
import sys
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


def record_saves(monkeypatch: Any, events: list[str]) -> None:
    """Append `"save"` to `events` each time the train stage's own `predictor.save()` returns.

    The real method still runs; the wrapper only notes when it finished, so a test can put the
    publish after it by the order of events rather than by what happens to be on disk. `fit()`
    calls `save()` itself several times on the way; those calls are not the stage's and are not
    recorded, so the event list shows exactly where the stage's line sits relative to its save.
    """
    from autogluon.tabular import TabularPredictor

    original = TabularPredictor.save

    def save(self: Any, *args: Any, **kwargs: Any) -> Any:
        caller = sys._getframe(1).f_globals.get("__name__")
        outcome = original(self, *args, **kwargs)
        if caller == train_module.__name__:
            events.append("save")
        return outcome

    monkeypatch.setattr(TabularPredictor, "save", save)


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
    events: list[str] = []
    inside = False
    record_saves(monkeypatch, events)

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
        events.append("publish")
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
    assert events == ["save", "publish"], "the call comes after save() has returned"
    model_files = {path for path in call.before if path.startswith(f"{result.predictor_key}/")}
    assert f"{result.predictor_key}/{PREDICTOR_FILE}" in model_files

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

    def __init__(self, root: Path, events: list[str] | None = None) -> None:
        super().__init__(root)
        self.events = events if events is not None else []
        self.published: list[tuple[str, dict[str, tuple[int, int, str]]]] = []

    def publish_local_path(self, key: str) -> tuple[str, ...]:
        # What an upload would carry: each file's content as it stands at the moment of the call,
        # not just its name, so a publish of pre-save versions differs from the final tree.
        self.events.append("publish")
        present = snapshot(self.local_path(key))
        self.published.append((key, present))
        return tuple(f"{key}/{name}" for name in present)

    def flush_local(self, prefix: str = "") -> tuple[str, ...]:
        return ()

    def release_local(self, prefix: str = "") -> None:
        return None


def test_on_a_mirrored_store_the_saved_predictor_is_published_once_before_the_scorer(
    tmp_path, monkeypatch
) -> None:
    events: list[str] = []
    record_saves(monkeypatch, events)
    storage = RecordingMirror(tmp_path / "data", events)
    assert isinstance(storage, SupportsLocalMirror)

    result = run_train(storage)

    assert len(storage.published) == 1, "the predictor is published exactly once"
    key, present = storage.published[0]
    assert key == result.predictor_key
    assert events == ["save", "publish"], "published after save() has returned"
    assert PREDICTOR_FILE in present
    assert SCORER_FILENAME not in present, "published before the scorer fit, which can still fail"
    # What was published is the final tree, file for file and byte for byte: the only file the
    # model directory gained afterwards is the scorer, which is written through the store, and no
    # published file changed after the call - so the bucket holds what the run ends with.
    after = snapshot(storage.local_path(result.predictor_key))
    assert set(after) == set(present) | {SCORER_FILENAME}
    assert {name: after[name] for name in present} == present
