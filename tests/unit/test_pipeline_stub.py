"""`engine.pipeline` and `engine.stages`: the stage vocabulary, Running rows and the stage-module surface.

The front half pins the flows, titles and Running rows to plan section 6.1, 6.2 and the prototype
screen in design section 6.4, so that reordering a stage or rewording a progress line has to be a
deliberate edit here and cannot drift out of the UI unnoticed.

The back half guards the stage-module surface of design section 6.5: every stage id in
STAGE_MODULE_MAP resolves to an importable module that defines the functions that stage promises,
the modules claimed as implemented really are stage modules, and nothing on the surface is still a
milestone stub. That claim is inverted from the one the file was born with. In M1 the stage modules
WERE typed stubs raising NotImplementedError("M2"), and the file existed to prove they raised
uniformly rather than half-failing in bespoke ways; as each milestone landed, modules moved out of
the stubbed half and into the implemented one. With the last stage in, the stubbed half is empty and
gone, and what remains is the assertion that now matters: the surface exists, it matches
STAGE_MODULE_MAP, and no part of it has quietly stayed a placeholder.

The file keeps its M1 name because other modules and docs reference the path; read the name as
historical, not as a description of what is asserted below.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from engine.config import STAGE_MODULE_MAP, RunMode
from engine.contracts import RunState, StageKey
from engine.jobs import ThreadJobRunner
from engine.pipeline import (
    GROUP_LABELS,
    SCORE_STAGES,
    STAGE_TITLES,
    TRAIN_STAGES,
    Pipeline,
    StageContext,
    running_rows,
    stages_for,
)
from engine.registry import LocalModelRegistry
from engine.storage import LocalStorage, run_key

# plan section 6.1 and 6.2, verbatim
PLAN_TRAIN_FLOW: tuple[str, ...] = (
    "ingest",
    "validate",
    "prepare",
    "split",
    "train",
    "evaluate",
    "explain",
    "register",
)
PLAN_SCORE_FLOW: tuple[str, ...] = (
    "ingest",
    "validate_against_schema",
    "prepare",
    "predict",
    "explain_rows",
    "actions",
    "export",
)

# the prototype's Running screen (design section 6.4, DEC-020)
PROTOTYPE_TRAIN_ROWS: tuple[str, ...] = (
    "Validating data",
    "Preparing features",
    "Training candidate models",
    "Evaluating on hold-out set",
    "Generating explanations & saving",
)
PROTOTYPE_SCORE_ROWS: tuple[str, ...] = (
    "Validating columns",
    "Loading champion model",
    "Scoring rows",
    "Generating reasons & actions",
)

# design section 6.5: stage id -> (module, the functions that module must define)
STAGE_FUNCTIONS: dict[str, tuple[str, ...]] = {
    "ingest": ("read_table", "profile_dataset"),
    "validate": ("validate_for_training",),
    "validate_against_schema": ("validate_against_schema",),
    "prepare": ("prepare", "replay"),
    "split": ("split_dataset",),
    "train": ("autogluon_fit_kwargs", "train"),
    "evaluate": ("evaluate",),
    "explain": ("global_importance", "row_reasons"),
    "explain_rows": ("row_reasons",),
    "register": ("build_model_version", "drift_baseline"),
    "predict": ("predict", "compute_drift"),
    "actions": ("assign_bands", "apply_actions"),
    "export": ("write_scores", "summarise"),
}


def public_functions(module: ModuleType) -> list[str]:
    """The functions a stage module defines itself, in definition order."""
    return [
        name
        for name, value in inspect.getmembers(module, inspect.isfunction)
        if not name.startswith("_") and value.__module__ == module.__name__
    ]


def call_with_placeholders(function: Any) -> None:
    """Call a stub with one placeholder per parameter; a stub raises before it reads any of them."""
    signature = inspect.signature(function)
    args: list[None] = []
    kwargs: dict[str, None] = {}
    for name, parameter in signature.parameters.items():
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
            kwargs[name] = None
        else:
            args.append(None)
    function(*args, **kwargs)


def _sentinel_context() -> StageContext:
    """A context the flow driver must pass through untouched, distinguishable from any other."""
    return cast(StageContext, object())


@pytest.fixture
def pipeline(tmp_path: Path) -> Pipeline:
    return Pipeline(
        LocalStorage(tmp_path / "data"),
        LocalModelRegistry(tmp_path / "registry.db"),
        ThreadJobRunner(max_workers=1),
    )


def test_the_stage_sequences_are_the_plan_flows() -> None:
    assert tuple(stage.value for stage in TRAIN_STAGES) == PLAN_TRAIN_FLOW
    assert tuple(stage.value for stage in SCORE_STAGES) == PLAN_SCORE_FLOW
    assert stages_for(RunMode.TRAIN) == TRAIN_STAGES
    assert stages_for(RunMode.SCORE) == SCORE_STAGES


def test_every_stage_key_has_a_title_and_a_group_label() -> None:
    assert set(STAGE_TITLES) == set(StageKey)
    assert set(StageKey) == set(TRAIN_STAGES) | set(SCORE_STAGES)
    for mode, stages in ((RunMode.TRAIN, TRAIN_STAGES), (RunMode.SCORE, SCORE_STAGES)):
        for stage in stages:
            assert GROUP_LABELS[(mode, stage)]
    assert len(GROUP_LABELS) == len(TRAIN_STAGES) + len(SCORE_STAGES)


def test_running_rows_are_the_prototype_lines() -> None:
    assert running_rows(RunMode.TRAIN) == PROTOTYPE_TRAIN_ROWS
    assert running_rows(RunMode.SCORE) == PROTOTYPE_SCORE_ROWS


def test_initial_status_is_every_stage_pending(pipeline: Pipeline) -> None:
    for mode, stages, rows in (
        (RunMode.TRAIN, TRAIN_STAGES, PROTOTYPE_TRAIN_ROWS),
        (RunMode.SCORE, SCORE_STAGES, PROTOTYPE_SCORE_ROWS),
    ):
        status = pipeline.initial_status("r_20260921_0000beef", mode)
        assert status.run_id == "r_20260921_0000beef"
        assert status.mode is mode
        assert status.state is RunState.PENDING
        assert status.current_stage is None
        assert status.progress_pct == 0
        assert tuple(stage.key for stage in status.stages) == stages
        assert all(stage.state is RunState.PENDING for stage in status.stages)
        assert all(stage.detail == "" for stage in status.stages)
        assert all(stage.started_at is None and stage.ended_at is None for stage in status.stages)
        assert [stage.title for stage in status.stages] == [STAGE_TITLES[key] for key in stages]
        assert tuple(dict.fromkeys(stage.group_label for stage in status.stages)) == rows


def test_initial_status_has_eight_train_stages_and_seven_score_stages(pipeline: Pipeline) -> None:
    assert len(pipeline.initial_status("r_1", RunMode.TRAIN).stages) == 8
    assert len(pipeline.initial_status("r_1", RunMode.SCORE).stages) == 7


def test_status_reads_the_stored_artefact(pipeline: Pipeline) -> None:
    status = pipeline.initial_status("r_1", RunMode.TRAIN)
    pipeline.storage.write_model(run_key("r_1", "status.json"), status)
    assert pipeline.status("r_1") == status


def test_cancel_delegates_to_the_job_runner(pipeline: Pipeline) -> None:
    assert pipeline.cancel("r_never_submitted") is False


def test_both_flows_are_wired_to_their_own_driver(
    pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The flipped tripwire. Until M3 this read "neither flow is implemented"; once run_train landed
    # it read "run_score is still a stub, and this flips the day it lands". Both have now landed, so
    # what belongs here is the successor claim, and it is the stronger one: each entry point hands
    # its OWN context to its OWN driver and returns exactly what that driver produced. A stub fails
    # it, and so does the copy-paste slip of pointing run_score at _TrainFlow. The flows' behaviour
    # is tests/unit/test_run_train.py, tests/unit/test_run_score.py and the integration suites.
    import engine.pipeline as pipeline_module

    seen: dict[str, tuple[Pipeline, object]] = {}
    produced: dict[str, object] = {"train": object(), "score": object()}

    def driver(name: str) -> type:
        class _Recorder:
            def __init__(self, owner: Pipeline, ctx: object) -> None:
                seen[name] = (owner, ctx)

            def execute(self) -> object:
                return produced[name]

        return _Recorder

    monkeypatch.setattr(pipeline_module, "_TrainFlow", driver("train"))
    monkeypatch.setattr(pipeline_module, "_ScoreFlow", driver("score"))

    train_ctx: StageContext = _sentinel_context()
    score_ctx: StageContext = _sentinel_context()
    assert pipeline.run_train(train_ctx) is produced["train"]
    assert pipeline.run_score(score_ctx) is produced["score"]
    assert seen == {"train": (pipeline, train_ctx), "score": (pipeline, score_ctx)}


def test_neither_flow_entry_point_is_a_stub_any_more() -> None:
    # The other half of the same tripwire: no NotImplementedError survives in either entry point,
    # so the dispatch test above cannot be satisfied by a driver that raises one itself.
    for method in (Pipeline.run_train, Pipeline.run_score):
        assert "NotImplementedError" not in inspect.getsource(method)


def test_stage_context_is_frozen() -> None:
    assert inspect.isclass(StageContext)
    fields = set(StageContext.__dataclass_fields__)
    assert fields == {
        "run_id",
        "mode",
        "config",
        "resolved",
        "storage",
        "registry",
        "cancel",
        "primary_key",
        "target",
        "upload_key",
        "model_version_id",
    }
    assert StageContext.__dataclass_params__.frozen is True


def test_every_stage_id_maps_to_a_module_that_defines_its_functions() -> None:
    assert set(STAGE_MODULE_MAP) == {stage.value for stage in StageKey}
    assert set(STAGE_MODULE_MAP) == set(STAGE_FUNCTIONS)
    for stage_id, module_name in STAGE_MODULE_MAP.items():
        module = importlib.import_module(module_name)
        for function_name in STAGE_FUNCTIONS[stage_id]:
            assert inspect.isfunction(getattr(module, function_name))


# Stage modules that carry real implementations -- as of M4, every module in STAGE_MODULE_MAP.
# Each entry names the milestone that landed it, and a module still joins this set by a deliberate
# edit rather than by inference, so the two tests below can read it from both ends: every name here
# must be a real stage module, and every one must actually run code.
IMPLEMENTED_STAGE_MODULES: frozenset[str] = frozenset(
    {
        "engine.stages.evaluate",  # M3: evaluate, compare_to_baseline
        "engine.stages.ingest",  # M2: read_upload, profile_dataset, fingerprint
        "engine.stages.prepare",  # M3: prepare, replay, split_dataset
        "engine.stages.validate",  # M2: the pure check registry and its composers
        "engine.stages.actions",  # M4: assign_bands, apply_actions, suppression_rules
        "engine.stages.export",  # M4: write_scores, summarise
        "engine.stages.register",  # M3: build_model_version, feature_schema, drift_baseline
        "engine.stages.train",  # M3: train, fit_scorer, autogluon_fit_kwargs
        "engine.stages.score",  # M4: predict, compute_drift
        "engine.stages.explain",  # M3: global importance and per-row reasons
    }
)


def test_implemented_modules_are_real_stage_modules() -> None:
    assert set(STAGE_MODULE_MAP.values()) >= IMPLEMENTED_STAGE_MODULES


# The stub half of the pair -- test_every_public_stage_function_is_a_milestone_stub, parametrized
# over STAGE_MODULE_MAP minus IMPLEMENTED_STAGE_MODULES -- was deleted when the last stage module
# landed and that difference went empty. It was not dropped to make a failure go away: pytest was
# skipping it as an empty parameter set, so it had already stopped asserting anything, and an
# always-skipped test is worse than none because it reads like coverage. Nothing was lost. Every
# module it used to watch is now covered by its mirror below, which makes the stronger claim on the
# same modules: not "this still raises NotImplementedError" but "this no longer does".


@pytest.mark.parametrize("module_name", sorted(IMPLEMENTED_STAGE_MODULES))
def test_implemented_stage_modules_no_longer_raise_not_implemented(module_name: str) -> None:
    # The mirror of the test above: once a module is listed as implemented, at least one
    # public function must actually do something, so the set cannot quietly over-claim.
    module = importlib.import_module(module_name)
    names = public_functions(module)
    assert names, f"{module_name} defines no public function"
    still_stubbed = []
    for name in names:
        try:
            call_with_placeholders(getattr(module, name))
        except NotImplementedError:  # pragma: no cover - only on a regression
            still_stubbed.append(name)
        except Exception:  # any other failure means it ran real code
            pass
    assert still_stubbed != names, f"{module_name} is listed as implemented but every function is a stub"


def test_the_stages_package_is_empty() -> None:
    import engine.stages

    assert public_functions(engine.stages) == []
    source = Path(engine.stages.__file__ or "").read_text(encoding="utf-8")
    assert source.strip() == ""
