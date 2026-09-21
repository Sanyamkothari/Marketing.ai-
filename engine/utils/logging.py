"""Logging setup for the engine.

Plan section 13.7: stage timings and row counts are logged at INFO, data values never are. Two
helpers carry that rule, one for each outcome, and neither has a parameter a customer value can
reach:

:func:`log_stage`
    a completed stage: its name, the rows it touched and how long it took.
:func:`log_failure`
    a failed one: the event and the exception's **class name**, never its message.

The distinction matters because an exception message is the most likely place a value leaks. A
library builds one out of whatever upset it - ``could not convert string to float: 'ACME-042'`` -
so ``logger.exception(...)`` and ``exc_info=True`` write a customer's cell into the log without
anyone deciding to. :class:`RedactingFormatter`, which :func:`configure_logging` installs, is the
last line of defence: it renders the frames of a traceback, which are code, and withholds every
exception message, which is data. It is defence in depth and not a licence - a call site that
hands an exception's text to the logger itself is still a leak, because a handler this module did
not configure (uvicorn's, a JSON handler in Phase 4) formats the record its own way.
"""

from __future__ import annotations

import logging
import traceback
from typing import TYPE_CHECKING, Final, TypeAlias

if TYPE_CHECKING:
    from types import TracebackType

    #: `logging`'s own `exc_info` triple, as typeshed declares it.
    _ExcInfo: TypeAlias = (
        "tuple[type[BaseException], BaseException, TracebackType | None] | tuple[None, None, None]"
    )

__all__ = [
    "WITHHELD",
    "RedactingFormatter",
    "configure_logging",
    "get_logger",
    "log_failure",
    "log_stage",
    "redacted_traceback",
]

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"

#: Stands in for every exception message the engine declines to write.
WITHHELD: Final[str] = "<message withheld: plan section 13.7>"

_CAUSE: Final[str] = "The above exception was the direct cause of the following exception:"
_CONTEXT: Final[str] = "During handling of the above exception, another exception occurred:"

_configured = False


def get_logger(name: str) -> logging.Logger:
    """Return the logger for ``name``; use ``__name__`` at the call site."""
    return logging.getLogger(name)


def configure_logging(level: str = "INFO") -> None:
    """Attach one console handler to the root logger and set its level.

    Calling this more than once is safe: the handler is attached on the first call only, so repeated
    calls (the API on startup, a script's ``main``) do not duplicate every line. The handler formats
    with :class:`RedactingFormatter`, so no line it writes can carry an exception's message.
    """
    global _configured
    root = logging.getLogger()
    if not _configured:
        handler = logging.StreamHandler()
        handler.setFormatter(RedactingFormatter(_FORMAT, datefmt=_DATEFMT))
        root.addHandler(handler)
        _configured = True
    root.setLevel(level.upper())


def log_stage(logger: logging.Logger, stage: str, *, rows: int | None, seconds: float) -> None:
    """Log one completed stage: its name, how many rows it touched and how long it took.

    ``rows`` is ``None`` for a stage that does not process rows, and is rendered as ``-``.
    """
    logger.info("stage=%s rows=%s seconds=%.3f", stage, "-" if rows is None else rows, seconds)


def log_failure(
    logger: logging.Logger, event: str, exc: BaseException, *, level: int = logging.WARNING
) -> None:
    """Log that ``event`` failed, naming the exception's class and nothing else.

    The failure-path counterpart to :func:`log_stage`, and the form ``engine.stages.validate``
    already uses by hand. A stage that wants a failure in the log calls this rather than
    ``logger.exception(...)`` or ``exc_info=True``: the class name says what went wrong without
    quoting the value that caused it (plan section 13.7).
    """
    logger.log(level, "%s error=%s", event, type(exc).__name__)


def redacted_traceback(exc: BaseException) -> str:
    """The frames of ``exc`` and of every exception it chains to, with each message withheld.

    A traceback is two things: where the failure happened, which is this repository's own code, and
    what the exception said, which a library routinely builds out of the value that upset it. The
    first is worth having in a log and the second is customer data, so the frames are kept verbatim
    and each ``Type: message`` line becomes ``Type: <message withheld>``.
    """
    links: list[BaseException] = []
    joiners: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        links.append(current)
        cause = current.__cause__
        context = None if current.__suppress_context__ else current.__context__
        following = cause if cause is not None else context
        if following is None or id(following) in seen:
            break
        joiners.append(_CAUSE if cause is not None else _CONTEXT)
        current = following

    # Python prints the innermost exception first, then the line that links it to the next one out.
    blocks = [_traceback_block(link) for link in reversed(links)]
    rendered = blocks[0]
    for joiner, block in zip(reversed(joiners), blocks[1:], strict=True):
        rendered += f"\n\n{joiner}\n\n{block}"
    return rendered


def _traceback_block(exc: BaseException) -> str:
    """One exception's frames, then its class name with the message withheld."""
    frames = "".join(traceback.format_tb(exc.__traceback__))
    kind = type(exc)
    name = (
        kind.__qualname__
        if kind.__module__ in {"builtins", "__main__"}
        else f"{kind.__module__}.{kind.__qualname__}"
    )
    return f"Traceback (most recent call last):\n{frames}{name}: {WITHHELD}"


class RedactingFormatter(logging.Formatter):
    """A :class:`logging.Formatter` that never renders an exception message.

    ``format`` drops any cached rendering first, so a record another handler has already formatted
    with the stock formatter is re-rendered here rather than reused - and the redacted text is what
    stays cached for whoever formats it next.
    """

    def formatException(self, ei: _ExcInfo) -> str:  # noqa: N802 - logging's own spelling
        """The traceback with every message withheld (:func:`redacted_traceback`)."""
        kind, exc, _tb = ei
        if exc is not None:
            return redacted_traceback(exc)
        return f"{'Exception' if kind is None else kind.__qualname__}: {WITHHELD}"

    def format(self, record: logging.LogRecord) -> str:
        """The record, rendered with a redacted traceback in place of any cached one."""
        record.exc_text = None
        return super().format(record)
