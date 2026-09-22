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

Phase 4a adds two things to that, and neither weakens it.

**A JSON rendering, for CloudWatch.** The choice is a `Settings` field rather than a logging
configuration file of its own, so that one frozen table of environment variables still describes
the whole deployment and a log format cannot be set somewhere `Settings.summary()` does not print
(DEC-380). ``MARKETING_AI_LOG_FORMAT=json`` selects
:class:`RedactingJsonFormatter`, which is a *subclass* of :class:`RedactingFormatter` and
deliberately does **not** override ``formatException``. There is therefore still exactly one code
path in this repository that turns an exception into text, and it is the one that withholds the
message: the guarantee is a class, not a convention, and a future third rendering inherits it for
free (DEC-381). The JSON body is built from a **closed allow-list** of keys rather than from
``record.__dict__``, so an attribute that some library or call site attaches to a record - a
``LoggerAdapter``'s payload, uvicorn's ``client_addr``, anything passed as ``extra=`` - cannot be
serialised by accident (DEC-382).

**A run's identity, carried by a `ContextVar`.** ``run_id``, ``stage`` and ``client_id`` reach a
record through :data:`CONTEXT_FIELDS` on a `contextvars.ContextVar`, read by :class:`ContextFilter`
which :func:`configure_logging` installs on the handler. Not a threaded logger and not a
`LoggerAdapter`: either of those would have to be passed down through every stage signature, and a
library logging on its own logger would be left without the identity. The filter sits at the sink,
so *every* record that reaches the handler is stamped, including one from pandas (DEC-383).

The binding is not inherited by a worker thread. ``ThreadPoolExecutor`` does not copy the calling
context into its workers, so ``with bind_log_context(run_id=...): runner.submit(...)`` stamps
nothing: the binding has to happen **inside the job body**, which is what
``tests/unit/test_logging_audit.py`` proves against a real `ThreadJobRunner` (DEC-384).
"""

from __future__ import annotations

import json
import logging
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Final, TypeAlias

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from types import TracebackType

    #: `logging`'s own `exc_info` triple, as typeshed declares it.
    _ExcInfo: TypeAlias = (
        "tuple[type[BaseException], BaseException, TracebackType | None] | tuple[None, None, None]"
    )

__all__ = [
    "CONTEXT_FIELDS",
    "JSON_FIELDS",
    "WITHHELD",
    "ContextFilter",
    "RedactingFormatter",
    "RedactingJsonFormatter",
    "bind_log_context",
    "configure_logging",
    "current_log_context",
    "get_logger",
    "install_context_filter",
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

CONTEXT_FIELDS: Final[tuple[str, ...]] = ("run_id", "stage", "client_id")
"""The identity a record carries besides its message.

Three identifiers, not a free-form mapping, and that is the point: a mapping would be somewhere a
caller could put a cell value without anyone reviewing it. Each of these three is an identifier
this product generates or an operator configures, never a value read out of a customer's file.
"""

JSON_FIELDS: Final[tuple[str, ...]] = (
    "time",
    "level",
    "logger",
    "message",
    *CONTEXT_FIELDS,
    "exception",
)
"""The **closed** list of keys :class:`RedactingJsonFormatter` may write. Adding one is deliberate.

An open rendering - ``record.__dict__`` minus a deny-list of the attributes `logging` itself sets -
fails in the direction that costs the most: an attribute nobody here has heard of is serialised, and
the unknown attribute is exactly the one most likely to hold a value (DEC-382).
"""

_handler: logging.Handler | None = None
"""The one handler this module owns. Every later `configure_logging` re-dresses it in place."""

_LOG_CONTEXT: ContextVar[Mapping[str, str]] = ContextVar("marketing_ai_log_context")


def get_logger(name: str) -> logging.Logger:
    """Return the logger for ``name``; use ``__name__`` at the call site."""
    return logging.getLogger(name)


def configure_logging(level: str = "INFO", *, log_format: str = "text") -> None:
    """Attach one console handler to the root logger, set its level and choose its rendering.

    Calling this more than once is safe: the handler is attached on the first call only, so repeated
    calls (the API on startup, a script's ``main``) do not duplicate every line. Every later call
    may still change the level and the rendering, because the formatter is set on each call rather
    than only on the first - that is what lets the API configure a placeholder logger at import time
    and re-configure it from `Settings` once they are loaded, without ending up with two handlers.

    One handler, re-dressed - never a second one per format. A process that configured text and
    then JSON would otherwise write every line twice, which is the kind of bug that is invisible on
    a laptop and doubles a log bill in an account (DEC-396).

    ``log_format`` takes `Settings.log_format`: ``"text"`` (the default, and byte-for-byte the
    Phase 1 line) or ``"json"``. Either way the formatter is a :class:`RedactingFormatter`, so no
    line the handler writes can carry an exception's message. An unrecognised value falls back to
    text rather than raising: `Settings` has already refused anything but these two, and a logging
    call is the wrong place to discover a configuration error.
    """
    global _handler
    root = logging.getLogger()
    if _handler is None:
        _handler = logging.StreamHandler()
        root.addHandler(_handler)
    formatter: RedactingFormatter = (
        RedactingJsonFormatter(datefmt=_DATEFMT)
        if log_format.strip().lower() == "json"
        else RedactingFormatter(_FORMAT, datefmt=_DATEFMT)
    )
    _handler.setFormatter(formatter)
    install_context_filter(_handler)
    root.setLevel(level.upper())


def install_context_filter(handler: logging.Handler) -> ContextFilter:
    """Ensure ``handler`` carries exactly one :class:`ContextFilter`, and return it.

    Idempotent, because :func:`configure_logging` is: calling it four times must not leave four
    filters stamping the same three attributes. Exposed because a handler this module did not build
    - uvicorn's, a test's capture handler - needs the same stamping to see a run's identity.
    """
    for existing in handler.filters:
        if isinstance(existing, ContextFilter):
            return existing
    added = ContextFilter()
    handler.addFilter(added)
    return added


def current_log_context() -> Mapping[str, str]:
    """The identity bound to this context: a mapping over some subset of :data:`CONTEXT_FIELDS`."""
    return _LOG_CONTEXT.get({})


@contextmanager
def bind_log_context(
    *, run_id: str | None = None, stage: str | None = None, client_id: str | None = None
) -> Iterator[None]:
    """Bind a run's identity for the duration of the block, then restore what was bound before.

    Nesting merges: a stage binds ``stage=`` inside a block that already bound ``run_id=``, and both
    reach the record. Passing ``None`` leaves that field as the enclosing block set it, so a stage
    cannot accidentally clear the run it belongs to.

    **This must be entered inside the job body, not around the submit.** A `ContextVar` is bound to
    the context that set it, and ``ThreadPoolExecutor`` starts its workers with a fresh context
    rather than a copy of the submitter's, so a binding wrapped around ``runner.submit(...)`` is
    gone by the time the job runs (DEC-384). `engine.jobs.ThreadJobRunner` takes a callable, so the
    correct shape is for that callable's first statement to be this context manager.
    """
    bound = {
        name: value
        for name, value in (("run_id", run_id), ("stage", stage), ("client_id", client_id))
        if value is not None
    }
    token = _LOG_CONTEXT.set({**_LOG_CONTEXT.get({}), **bound})
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


class ContextFilter(logging.Filter):
    """Stamps :data:`CONTEXT_FIELDS` onto every record from the `ContextVar`, or ``None``.

    Installed on the *handler* rather than on a logger, because a filter on a logger does not see a
    record that propagated up from a child - and the records worth stamping include the ones pandas
    and botocore write on loggers this repository does not own (DEC-383).

    It overwrites rather than defers to an attribute already on the record. That is deliberate: the
    `ContextVar` is the single source of a run's identity, and a record carrying its own ``run_id``
    from somewhere else is a second mechanism nobody has audited.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Stamp the record and keep it; this filter never drops anything."""
        context = _LOG_CONTEXT.get({})
        for name in CONTEXT_FIELDS:
            setattr(record, name, context.get(name))
        return True


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


class RedactingJsonFormatter(RedactingFormatter):
    """One JSON object per line, for CloudWatch Logs Insights - built from a closed allow-list.

    Two properties make this safe, and both are structural rather than a habit someone has to keep.

    **It does not override** ``formatException``. The only code in this repository that turns an
    exception into text is still :meth:`RedactingFormatter.formatException`, which keeps the frames
    and withholds every message. A subclass that reimplemented it - "just to put the traceback in a
    field" - would reintroduce the leak in a place nobody would think to audit, so it is left
    inherited on purpose and `tests/unit/test_logging_audit.py` asserts that it is (DEC-381).

    **It never reads** ``record.__dict__``. The payload is assembled key by key from
    :data:`JSON_FIELDS`; an attribute attached to the record by a `LoggerAdapter`, by ``extra=``, by
    uvicorn or by a library is not serialised, because there is no code path that would serialise
    it. The cost is that a new field has to be added here by hand. That is the point (DEC-382).

    ``message`` is ``record.getMessage()``, exactly what the text formatter renders, so the audit
    in `tests/unit/test_logging_audit.py` proves the same thing about both paths from one set of
    assertions. ``exception`` is absent - not null - when the record carries no exception, which
    keeps a successful line short in a log store billed by ingested bytes.
    """

    def __init__(self, datefmt: str | None = None) -> None:
        """No ``fmt``: the format string belongs to the text rendering and means nothing here."""
        super().__init__(datefmt=datefmt)

    def format(self, record: logging.LogRecord) -> str:
        """The record as one JSON object, with a redacted traceback and no unexpected key."""
        record.exc_text = None
        payload: dict[str, Any] = {
            "time": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in CONTEXT_FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = str(value)
        if record.exc_info is not None:
            # The inherited, redacting implementation - never a local rendering of the exception.
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps({key: payload[key] for key in JSON_FIELDS if key in payload})
