"""Engine failures: the pipeline's own exception, and one way to turn any failure into a `RunError`.

Every stage module already raises a *coded* exception of its own - `ingest.IngestError`,
`scorer.TrainError`, `evaluate.EvaluationError`, `registry.RegistryError`, `storage.StorageError`,
`config.ConfigError` - each carrying a machine-readable `code` and a business-language `message`
(plan section 13.4). This module does not replace them and does not restate their message texts,
which would only let the two copies drift apart. It adds the two things the pipeline needs:

* `EngineError`, for the failures the **pipeline itself** decides on rather than a stage: a run
  blocked by validation, and a stage asked to run before the one that feeds it. Its codes, messages
  and suggestions are the whole of `ENGINE_ERRORS`, so `docs/API.md` can grow a section from this
  one table;
* :func:`run_error`, which turns *any* exception into the `RunError` that goes into `status.json`
  and `run.json`. A coded exception keeps its own code and message, whoever raised it. Anything
  else is a bug rather than a handled condition, so it becomes `STAGE_FAILED` with a message that
  names the stage and says nothing else - the traceback belongs in the log, never in the artefact a
  user reads. (DEC-072.)

`RunError` carries a code, a message and a stage but no suggestion, so `EngineError.suggestion`
reaches the log rather than the screen; it is here because plan section 13.4 asks every engine
error to have one, and because the API will want it the moment a failed run grows a help line.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from engine.contracts import RunError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from engine.contracts import StageKey


__all__ = [
    "ENGINE_ERRORS",
    "JOB_FAILED_REMOTELY",
    "JOB_SPEC_UNREADABLE",
    "JOB_SUBMIT_FAILED",
    "RUN_BLOCKED_BY_VALIDATION",
    "STAGE_FAILED",
    "STAGE_OUT_OF_ORDER",
    "EngineError",
    "engine_error",
    "run_error",
]


RUN_BLOCKED_BY_VALIDATION: Final[str] = "RUN_BLOCKED_BY_VALIDATION"
"""Validation found an error-severity problem, so no model may be trained on this file."""

STAGE_FAILED: Final[str] = "STAGE_FAILED"
"""A stage failed in a way no stage anticipated: a bug, reported without its traceback."""

STAGE_OUT_OF_ORDER: Final[str] = "STAGE_OUT_OF_ORDER"
"""A stage ran before the stage that produces its input. Only a wiring mistake reaches this."""

JOB_SUBMIT_FAILED: Final[str] = "JOB_SUBMIT_FAILED"
"""The compute service refused the job, so no stage ever ran (DEC-331)."""

JOB_FAILED_REMOTELY: Final[str] = "JOB_FAILED_REMOTELY"
"""The compute ended without the run writing an ending; reconciliation wrote this one (DEC-325)."""

JOB_SPEC_UNREADABLE: Final[str] = "JOB_SPEC_UNREADABLE"
"""The container could not read the `job_spec.json` it was pointed at, so it ran nothing (DEC-328)."""


ENGINE_ERRORS: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        RUN_BLOCKED_BY_VALIDATION: (
            "The data did not pass validation, so the run stopped before training anything.",
            "Open the Data page, fix the problems listed there and start the run again.",
        ),
        STAGE_FAILED: (
            "{stage} failed unexpectedly.",
            "Try the run again. If it fails the same way, send the run id to support.",
        ),
        STAGE_OUT_OF_ORDER: (
            "{what} is not available, because an earlier stage did not produce it.",
            "This is an engine fault rather than a problem with the data; send the run id to support.",
        ),
        JOB_SUBMIT_FAILED: (
            "The run could not be started on the compute service, so nothing has run yet.",
            "Start the run again in a few minutes. If it keeps failing, send the run id to support.",
        ),
        JOB_FAILED_REMOTELY: (
            "The run stopped because the compute running it ended before the work finished.",
            "Start the run again. If it stops the same way, send the run id to support.",
        ),
        JOB_SPEC_UNREADABLE: (
            "The run could not be started, because the description of the work could not be read.",
            "Start the run again; this one changed nothing. If it happens again, send the run id to support.",
        ),
    }
)
"""Code -> (message template, suggestion) for the failures the pipeline itself raises."""


class EngineError(Exception):
    """A failure the pipeline decided on, with a code the UI can switch on and a message it can show."""

    def __init__(
        self, code: str, message: str, *, suggestion: str = "", stage: StageKey | None = None
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.suggestion = suggestion if suggestion else ENGINE_ERRORS.get(code, ("", ""))[1]
        self.stage = stage


def engine_error(code: str, *, stage: StageKey | None = None, **values: object) -> EngineError:
    """Build the `EngineError` for `code` from `ENGINE_ERRORS`, filling the message's placeholders."""
    message, suggestion = ENGINE_ERRORS[code]
    return EngineError(code, message.format(**values), suggestion=suggestion, stage=stage)


def run_error(exc: BaseException, *, stage: StageKey, title: str) -> RunError:
    """The `RunError` for `exc`, raised while `stage` (rendered as `title`) was running.

    An exception that carries a `code` and a `message` - every coded engine exception does - keeps
    both, so the user reads the words the stage that knows the failure chose. Everything else is a
    bug: it becomes `STAGE_FAILED` naming the stage, and its traceback goes to the log instead.
    """
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None)
    if isinstance(code, str) and isinstance(message, str) and code and message:
        return RunError(code=code, message=message, stage=stage)
    return RunError(
        code=STAGE_FAILED, message=ENGINE_ERRORS[STAGE_FAILED][0].format(stage=title), stage=stage
    )
