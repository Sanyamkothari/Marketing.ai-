"""Shared by every connector test module (base, Shopify, Klaviyo, API).

Registers the `live_connector` marker: tests that talk to a real sandbox account. They are skipped
unless the environment variables they name are set (see `live_env`), so CI never needs credentials.
The repository runs pytest with `--strict-markers`, and `pyproject.toml` is a shared file, so the
marker is registered here rather than there.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "live_connector: talks to a real platform sandbox; skipped unless its credential variables are set",
    )


@pytest.fixture
def live_env() -> Callable[..., dict[str, str]]:
    """`live_env("AUTONOMY_LIVE_X", ...)` returns those variables, or skips the test naming the missing ones."""

    def need(*names: str) -> dict[str, str]:
        missing = [n for n in names if not os.environ.get(n, "").strip()]
        if missing:
            pytest.skip(f"live connector test: set {', '.join(missing)} to run it")
        return {n: os.environ[n].strip() for n in names}

    return need
