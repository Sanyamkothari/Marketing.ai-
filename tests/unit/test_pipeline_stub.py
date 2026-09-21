"""`engine.pipeline` and `engine.stages`: the M1 stage vocabulary, Running rows and typed stubs."""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from types import ModuleType
from typing import Any

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


def test_the_two_flows_are_not_implemented_yet(pipeline: Pipeline) -> None:
    context: StageContext = None  # type: ignore[assignment]
    with pytest.raises(NotImplementedError, match="M3"):
        pipeline.run_train(context)
    with pytest.raises(NotImplementedError, match="M4"):
        pipeline.run_score(context)


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


# Stage modules that now carry real implementations. A module moves into this set
# deliberately, when its milestone lands, so neither half of the pair below can go
# stale: a stubbed module must still raise, and an implemented one must not.
IMPLEMENTED_STAGE_MODULES: frozenset[str] = frozenset(
    {
        "engine.stages.prepare",  # M3: prepare, replay, split_dataset
        "engine.stages.actions",  # M4: assign_bands, apply_actions, suppression_rules
        "engine.stages.export",  # M4: write_scores, summarise
        "engine.stages.register",  # M3: build_model_version, feature_schema, drift_baseline
        "engine.stages.score",  # M4: compute_drift (predict is still an M4 stub)
    }
)
STUBBED_STAGE_MODULES: frozenset[str] = frozenset(STAGE_MODULE_MAP.values()) - IMPLEMENTED_STAGE_MODULES


def test_implemented_modules_are_real_stage_modules() -> None:
    assert set(STAGE_MODULE_MAP.values()) >= IMPLEMENTED_STAGE_MODULES


@pytest.mark.parametrize("module_name", sorted(STUBBED_STAGE_MODULES))
def test_every_public_stage_function_is_a_milestone_stub(module_name: str) -> None:
    module = importlib.import_module(module_name)
    names = public_functions(module)
    assert names, f"{module_name} defines no public function"
    for name in names:
        with pytest.raises(NotImplementedError, match=r"^M[0-9]$"):
            call_with_placeholders(getattr(module, name))


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
