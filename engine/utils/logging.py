"""Logging setup for the engine.

Plan §13.7: stage timings and row counts are logged at INFO, data values never are. :func:`log_stage`
is the only logging helper the stages use, and its signature admits nothing but a stage name, a row
count and a duration, so there is no parameter through which a customer value could reach a log line.
"""

from __future__ import annotations

import logging

__all__ = ["configure_logging", "get_logger", "log_stage"]

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"

_configured = False


def get_logger(name: str) -> logging.Logger:
    """Return the logger for ``name``; use ``__name__`` at the call site."""
    return logging.getLogger(name)


def configure_logging(level: str = "INFO") -> None:
    """Attach one console handler to the root logger and set its level.

    Calling this more than once is safe: the handler is attached on the first call only, so repeated
    calls (the API on startup, a script's ``main``) do not duplicate every line.
    """
    global _configured
    root = logging.getLogger()
    if not _configured:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
        root.addHandler(handler)
        _configured = True
    root.setLevel(level.upper())


def log_stage(logger: logging.Logger, stage: str, *, rows: int | None, seconds: float) -> None:
    """Log one completed stage: its name, how many rows it touched and how long it took.

    ``rows`` is ``None`` for a stage that does not process rows, and is rendered as ``-``.
    """
    logger.info("stage=%s rows=%s seconds=%.3f", stage, "-" if rows is None else rows, seconds)
