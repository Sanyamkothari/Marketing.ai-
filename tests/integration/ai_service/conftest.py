from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake services are on 127.0.0.1; a sandbox's proxy variables must not intercept them."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    """`create_app(settings=...)` configures the process's logging; a test is not a process.

    Left in place, the handler and its redacting formatter would change how every later test in the
    session logs, which `tests/unit/test_logging_audit.py` makes assertions about (DEC-381, DEC-383);
    `tests/integration/test_jobs_as_sagemaker.py` restores it the same way.
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
