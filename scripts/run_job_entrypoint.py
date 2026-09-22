"""What a SageMaker container runs: read the job spec, run the flow, report the outcome.

This is the other half of DEC-324. `api/routes/runs.py` writes `job_spec.json` into the artefact
store and `engine.aws.sagemaker_jobs.SageMakerJobRunner` ships its **key** in the container's
environment; this module reads that document back and calls `engine.runs.run_job_spec` - the same
function the local closure calls on a thread of the API process. There is no second definition of
the work here, and there is deliberately nothing in this file that decides anything about a run:
every choice was made when the spec was written.

Three things it does have to do, because it is a process and not a closure:

**Build the services from the environment.** `engine.settings.build_services` is the one place that
turns a described deployment into a `Storage` and a `ModelRegistry`, and it is the same call
`api/deps.py` makes - so a container and an API task can never disagree about where the artefacts
are (DEC-308).

**Turn SIGTERM into a cancellation.** `StopTrainingJob` sends SIGTERM and then waits before killing
the container. Handled, that becomes exactly the cooperative cancellation DEC-017 already
specifies: the token is set, the running stage stops at its next poll, and the pipeline writes the
run as cancelled on the stage it stopped at. Unhandled, it would be a run that says "running" until
somebody reconciles it.

**Write the failure file.** SageMaker reads `/opt/ml/output/failure` and shows the first line of it
as the job's `FailureReason`. What goes in it is the run's *coded* message and nothing else - never
an exception's text, which is the most likely place a value out of the customer's file appears
(plan section 13.7). The stage-level truth is already in `status.json`; this file exists so the
console and the runner agree that the job failed (DEC-328).

Exit codes are the job's outcome as the service reads it: `0` done, `1` failed, `2` cancelled -
`StopTrainingJob` is what asked for that last one, so exiting non-zero is the honest answer rather
than pretending the work finished.
"""

from __future__ import annotations

import argparse
import os
import signal
import threading
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from types import FrameType
from typing import Final

from engine.errors import ENGINE_ERRORS, JOB_SPEC_UNREADABLE, STAGE_FAILED
from engine.jobs import CancelToken, JobCancelledError
from engine.runs import read_job_spec, run_job_spec
from engine.settings import Settings, build_services
from engine.utils.logging import bind_log_context, configure_logging, get_logger, log_failure

__all__ = ["DEFAULT_FAILURE_PATH", "FAILURE_PATH_ENV_VAR", "main", "write_failure_file"]

_LOGGER = get_logger("scripts.run_job_entrypoint")

COMMAND: Final[str] = "python -m scripts.run_job_entrypoint"

JOB_SPEC_KEY_ENV_VAR: Final[str] = "MARKETING_AI_JOB_SPEC_KEY"
"""The storage key of this job's `job_spec.json`; the whole contract with the runner (DEC-328)."""

FAILURE_PATH_ENV_VAR: Final[str] = "MARKETING_AI_FAILURE_PATH"
DEFAULT_FAILURE_PATH: Final[str] = "/opt/ml/output/failure"
"""Where SageMaker reads a training job's failure message. Not chosen: the documented path.

It is overridable through the environment because this module is run as a plain process in the
tests, and a test that had to be able to write `/opt/ml` would be a test nobody can run.
"""

EXIT_OK: Final[int] = 0
EXIT_FAILED: Final[int] = 1
EXIT_CANCELLED: Final[int] = 2
"""The three outcomes, as exit codes. Distinct so a stopped job is not filed as a broken one."""

NO_SPEC_KEY: Final[str] = (
    f"{JOB_SPEC_KEY_ENV_VAR} is not set, so this container was not told which job to run."
)


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    cancel: CancelToken | None = None,
) -> int:
    """Run the job this container was given; returns the process exit code.

    `environ` is a parameter rather than a read of `os.environ` so that the whole entrypoint can be
    exercised in-process by a test that is pretending to be SageMaker, with the environment the
    runner actually put in the request - which is the only way to find out whether that environment
    is sufficient (DEC-337).

    `cancel` is the same seam for the other half of the contract. In a container the token is made
    here and SIGTERM sets it; on a worker thread no signal can be delivered at all
    (`_install_stop_handler` explains why), so a caller that has to be able to stop this job passes
    the token it will set. Production passes neither argument and is unaffected by both.
    """
    parser = argparse.ArgumentParser(prog=COMMAND, description="Run one marketing-ai job.")
    parser.add_argument(
        "--spec-key",
        default=None,
        help=f"storage key of job_spec.json; defaults to ${JOB_SPEC_KEY_ENV_VAR}",
    )
    args = parser.parse_args(argv)
    env: Mapping[str, str] = os.environ if environ is None else environ

    settings = Settings.load(env)
    configure_logging(settings.log_level, log_format=settings.log_format)
    spec_key = args.spec_key or env.get(JOB_SPEC_KEY_ENV_VAR, "")
    failure_path = Path(env.get(FAILURE_PATH_ENV_VAR, DEFAULT_FAILURE_PATH))
    if not spec_key:
        _LOGGER.error("job.no_spec_key")
        write_failure_file(failure_path, NO_SPEC_KEY)
        return EXIT_FAILED

    storage, registry = build_services(settings)
    try:
        spec = read_job_spec(storage, spec_key)
    except Exception as exc:  # any store, any reason: the container cannot run what it cannot read
        log_failure(_LOGGER, "job.spec", exc)
        write_failure_file(failure_path, ENGINE_ERRORS[JOB_SPEC_UNREADABLE][0])
        return EXIT_FAILED

    token = CancelToken() if cancel is None else cancel
    _install_stop_handler(token)
    with bind_log_context(run_id=spec.run_id, client_id=settings.client_id):
        _LOGGER.info("job.start entrypoint=%s backend=%s", spec.entrypoint.value, spec.backend)
        try:
            run_job_spec(spec, token, storage=storage, registry=registry)
        except JobCancelledError:
            _LOGGER.info("job.cancelled")
            write_failure_file(failure_path, "The run was cancelled.")
            return EXIT_CANCELLED
        except Exception as exc:
            log_failure(_LOGGER, "job.failed", exc)
            write_failure_file(failure_path, _failure_message(exc))
            return EXIT_FAILED
        _LOGGER.info("job.done")
    return EXIT_OK


def _failure_message(exc: BaseException) -> str:
    """The one line SageMaker will show, by `engine.errors.run_error`'s rule and without its stage.

    A coded exception keeps its own message, which is the same sentence `status.json` now carries,
    so the console and the artefact say the same thing. Anything else is a bug rather than a handled
    condition and becomes the generic line: the traceback is in the log, and the message of an
    uncoded exception is exactly what must not be copied anywhere a person reads it (plan 13.7).

    `run_error` itself is not called because it requires the stage that failed, and a failure that
    reached this far has none to name - the flow that knew it already wrote it into `status.json`.
    """
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None)
    if isinstance(code, str) and isinstance(message, str) and code and message:
        return message
    return ENGINE_ERRORS[STAGE_FAILED][0].format(stage="The run")


def write_failure_file(path: Path, message: str) -> None:
    """Write SageMaker's failure file, if the directory it belongs in exists.

    Missing directory means "not running as a SageMaker job" - a local `docker run`, a test - and
    creating `/opt/ml/output` there would be this process pretending to be somewhere it is not. The
    message is already the coded one; nothing is added to it here.
    """
    try:
        if not path.parent.is_dir():
            return
        path.write_text(f"{message}\n", encoding="utf-8")
    except OSError as exc:  # a failure we cannot report is still a failure; the exit code carries it
        log_failure(_LOGGER, "job.failure_file", exc)


def _install_stop_handler(cancel: CancelToken) -> None:
    """Make SIGTERM set the cancel token, when this process is in a position to handle signals.

    Only the main thread of the main interpreter may install a handler, and this module is run on a
    worker thread by the test that pretends to be SageMaker. That test cancels through the token
    directly, so skipping the handler there costs nothing and keeps the production path - where this
    *is* the main thread - exactly as it should be.
    """
    if threading.current_thread() is not threading.main_thread():
        return

    def handler(signum: int, frame: FrameType | None) -> None:
        del frame
        _LOGGER.info("job.stop_requested signal=%s", signum)
        cancel.cancel()

    for received in (signal.SIGTERM, signal.SIGINT):
        with suppress(ValueError, OSError):  # a platform without the signal is not a failure
            signal.signal(received, handler)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
