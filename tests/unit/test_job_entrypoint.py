"""`scripts/run_job_entrypoint.py`: what a container does with one environment variable.

The whole contract between `SageMakerJobRunner` and the image is `MARKETING_AI_JOB_SPEC_KEY` plus
a described deployment (DEC-328), so every test here drives `main()` through an explicit `environ`
mapping - never the process environment. That is not only hygiene: it is the only way to find out
whether the mapping the runner puts in the request is *sufficient*, which is the question
`tests/integration/test_jobs_as_sagemaker.py` then asks with the real one.

Three properties matter more than the rest:

* **The exit code is the outcome.** `0` done, `1` failed, `2` cancelled. A stopped job filed as a
  broken one would page somebody for a button a user pressed.
* **The failure file never carries an exception's message.** SageMaker shows its first line in the
  console as `FailureReason`, and a library's message is the most likely place a value out of the
  customer's file appears (plan section 13.7).
* **It decides nothing about the run.** Every choice was made when the spec was written; this
  module reads it back and calls the same `run_job_spec` the local closure calls (DEC-324).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from engine.contracts import JobSpec, RunState, RunStatus
from engine.errors import ENGINE_ERRORS, JOB_SPEC_UNREADABLE, STAGE_FAILED
from engine.jobs import CancelToken, JobCancelledError
from engine.pipeline import STATUS_FILENAME
from engine.runs import job_spec_key, write_job_spec
from engine.settings import ENV_VAR_FOR_FIELD
from engine.storage import LocalStorage, run_key
from scripts.run_job_entrypoint import (
    DEFAULT_FAILURE_PATH,
    EXIT_CANCELLED,
    EXIT_FAILED,
    EXIT_OK,
    FAILURE_PATH_ENV_VAR,
    JOB_SPEC_KEY_ENV_VAR,
    NO_SPEC_KEY,
    main,
    write_failure_file,
)
from tests.unit.test_runs_module import _Upload, make_run

CODED_MESSAGE = "The data did not pass validation, so the run stopped before training anything."


class _CodedError(Exception):
    """A coded engine exception: the shape every stage raises (plan section 13.4)."""

    code = "RUN_BLOCKED_BY_VALIDATION"
    message = CODED_MESSAGE


class _UncodedError(Exception):
    """A bug, whose message must never be copied anywhere a person reads it."""


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """Put this process's logging back exactly as it was found, after every test in this module.

    `configure_logging` is called for real here - by `main()` in a container, and by `create_app`
    when it is given settings - because configuring the process is part of what is under test. A
    test is not a process, though: the handler, its formatter and its `ContextFilter` would
    otherwise outlive this module and change how every later test in the session logs, which
    `tests/unit/test_logging_audit.py` makes assertions about (DEC-381, DEC-383).
    """
    root = logging.getLogger()
    handlers = list(root.handlers)
    state = [(handler, list(handler.filters), handler.formatter) for handler in handlers]
    level = root.level
    try:
        yield
    finally:
        root.handlers = handlers
        root.setLevel(level)
        for handler, filters, formatter in state:
            handler.filters = filters
            handler.setFormatter(formatter)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "output").mkdir()
    return tmp_path


@pytest.fixture
def environ(workspace: Path) -> dict[str, str]:
    """The environment a container is handed: a described deployment plus this job's spec key."""
    return {
        ENV_VAR_FOR_FIELD["data_dir"]: str(workspace / "data"),
        ENV_VAR_FOR_FIELD["log_level"]: "WARNING",
        FAILURE_PATH_ENV_VAR: str(workspace / "output" / "failure"),
    }


@pytest.fixture
def spec(workspace: Path, config_root: Path) -> JobSpec:
    """A real run directory in the store the environment above points at."""
    from engine.config import get_catalog, resolve_config
    from engine.registry import LocalModelRegistry
    from engine.runs import job_spec_for

    storage = LocalStorage(workspace / "data")
    record = make_run(
        storage,
        LocalModelRegistry(workspace / "data" / "registry.db"),
        resolve_config("targeted-advertisement", {}, root=config_root),
        get_catalog(config_root),
    )
    built = job_spec_for(record, upload=_Upload())
    write_job_spec(storage, built)
    return built


def failure_text(workspace: Path) -> str:
    return (workspace / "output" / "failure").read_text(encoding="utf-8")


def patch_flow(monkeypatch: pytest.MonkeyPatch, body: Any) -> list[JobSpec]:
    """Replace the flow `run_job_spec` dispatches to, keeping everything around it real."""
    seen: list[JobSpec] = []

    def run(spec: JobSpec, cancel: CancelToken, *, storage: Any, registry: Any) -> None:
        del storage, registry
        seen.append(spec)
        body(cancel)

    monkeypatch.setattr("scripts.run_job_entrypoint.run_job_spec", run)
    return seen


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_the_container_reads_the_spec_it_was_pointed_at_and_runs_it(
    environ: dict[str, str], spec: JobSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    environ[JOB_SPEC_KEY_ENV_VAR] = job_spec_key(spec.run_id)
    seen = patch_flow(monkeypatch, lambda cancel: None)

    assert main([], environ) == EXIT_OK
    assert [item.run_id for item in seen] == [spec.run_id]
    assert seen[0] == spec, "nothing is decided here; the spec is read back as it was written"


def test_the_spec_key_can_also_be_a_command_line_argument(
    environ: dict[str, str], spec: JobSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = patch_flow(monkeypatch, lambda cancel: None)
    assert main(["--spec-key", job_spec_key(spec.run_id)], environ) == EXIT_OK
    assert len(seen) == 1


def test_a_successful_job_writes_no_failure_file(
    environ: dict[str, str], spec: JobSpec, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environ[JOB_SPEC_KEY_ENV_VAR] = job_spec_key(spec.run_id)
    patch_flow(monkeypatch, lambda cancel: None)

    assert main([], environ) == EXIT_OK
    assert not (workspace / "output" / "failure").exists()


# ---------------------------------------------------------------------------
# The three ways a job ends
# ---------------------------------------------------------------------------
def test_a_failed_job_exits_one_and_reports_the_stage_s_own_words(
    environ: dict[str, str], spec: JobSpec, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The console and `status.json` then say the same thing, because it is the same sentence."""
    environ[JOB_SPEC_KEY_ENV_VAR] = job_spec_key(spec.run_id)
    patch_flow(monkeypatch, _raise(_CodedError()))

    assert main([], environ) == EXIT_FAILED
    assert failure_text(workspace).strip() == CODED_MESSAGE


def test_an_uncoded_failure_is_generic_and_never_quotes_the_exception(
    environ: dict[str, str], spec: JobSpec, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environ[JOB_SPEC_KEY_ENV_VAR] = job_spec_key(spec.run_id)
    patch_flow(monkeypatch, _raise(_UncodedError("could not convert string to float: 'ACME-042'")))

    assert main([], environ) == EXIT_FAILED
    written = failure_text(workspace)
    assert "ACME-042" not in written
    assert written.strip() == ENGINE_ERRORS[STAGE_FAILED][0].format(stage="The run")


def test_a_cancelled_job_exits_two_rather_than_pretending_it_finished(
    environ: dict[str, str], spec: JobSpec, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`StopTrainingJob` asked for this, so a non-zero exit is the honest answer."""
    environ[JOB_SPEC_KEY_ENV_VAR] = job_spec_key(spec.run_id)
    patch_flow(monkeypatch, _raise(JobCancelledError("The run was cancelled.")))

    assert main([], environ) == EXIT_CANCELLED
    assert "cancelled" in failure_text(workspace)


def _raise(exc: BaseException) -> Any:
    def body(cancel: CancelToken) -> None:
        del cancel
        raise exc

    return body


def test_the_three_exit_codes_are_distinct() -> None:
    assert len({EXIT_OK, EXIT_FAILED, EXIT_CANCELLED}) == 3


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------
def test_a_caller_that_has_to_stop_the_job_passes_the_token_it_will_set(
    environ: dict[str, str], spec: JobSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a worker thread no signal can be delivered, so the token is the seam that is left."""
    environ[JOB_SPEC_KEY_ENV_VAR] = job_spec_key(spec.run_id)
    token = CancelToken()

    def body(cancel: CancelToken) -> None:
        assert cancel is token
        cancel.raise_if_cancelled()

    patch_flow(monkeypatch, body)
    token.cancel()

    assert main([], environ, cancel=token) == EXIT_CANCELLED


# ---------------------------------------------------------------------------
# Being told nothing, or something unreadable
# ---------------------------------------------------------------------------
def test_a_container_that_was_not_told_which_job_to_run_says_so(
    environ: dict[str, str], workspace: Path
) -> None:
    assert main([], environ) == EXIT_FAILED
    assert failure_text(workspace).strip() == NO_SPEC_KEY
    assert JOB_SPEC_KEY_ENV_VAR in failure_text(workspace)


def test_a_spec_that_cannot_be_read_is_reported_with_its_own_code(
    environ: dict[str, str], workspace: Path
) -> None:
    environ[JOB_SPEC_KEY_ENV_VAR] = "runs/r_nothing/job_spec.json"

    assert main([], environ) == EXIT_FAILED
    assert failure_text(workspace).strip() == ENGINE_ERRORS[JOB_SPEC_UNREADABLE][0]


def test_a_spec_that_is_not_a_spec_is_reported_the_same_way(environ: dict[str, str], workspace: Path) -> None:
    storage = LocalStorage(workspace / "data")
    storage.write_text("runs/r_x/job_spec.json", json.dumps({"job_id": "r_x"}))
    environ[JOB_SPEC_KEY_ENV_VAR] = "runs/r_x/job_spec.json"

    assert main([], environ) == EXIT_FAILED
    assert failure_text(workspace).strip() == ENGINE_ERRORS[JOB_SPEC_UNREADABLE][0]


def test_nothing_about_the_run_is_touched_when_the_spec_cannot_be_read(
    environ: dict[str, str], spec: JobSpec, workspace: Path
) -> None:
    """ "This one changed nothing" is what the suggestion promises; it has to be true."""
    environ[JOB_SPEC_KEY_ENV_VAR] = "runs/r_nothing/job_spec.json"
    before = LocalStorage(workspace / "data").read_model(run_key(spec.run_id, STATUS_FILENAME), RunStatus)

    main([], environ)

    after = LocalStorage(workspace / "data").read_model(run_key(spec.run_id, STATUS_FILENAME), RunStatus)
    assert after == before
    assert after.state is RunState.PENDING


# ---------------------------------------------------------------------------
# The failure file itself
# ---------------------------------------------------------------------------
def test_the_documented_path_is_the_default() -> None:
    """Not chosen: `/opt/ml/output/failure` is where SageMaker reads it."""
    assert DEFAULT_FAILURE_PATH == "/opt/ml/output/failure"


def test_a_missing_output_directory_means_this_is_not_a_sagemaker_job(tmp_path: Path) -> None:
    """Creating `/opt/ml/output` would be this process pretending to be somewhere it is not."""
    target = tmp_path / "not-there" / "failure"
    write_failure_file(target, "anything")
    assert not target.exists()


def test_the_message_is_written_whole_with_one_trailing_newline(tmp_path: Path) -> None:
    target = tmp_path / "failure"
    write_failure_file(target, "The run was cancelled.")
    assert target.read_text(encoding="utf-8") == "The run was cancelled.\n"
