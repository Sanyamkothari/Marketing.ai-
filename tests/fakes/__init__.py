"""Fakes that stand in for a service, held to the same bar as the engine itself.

`tests/fakes` is in `[tool.mypy].files`, unlike the rest of `tests/`. The precedent is DEC-041,
which type-checks `tests/fixtures/make_data.py` for the same reason: a fake that stands in for
SageMaker decides whether an integration test is testing anything at all, so it is test
*infrastructure* rather than a test, and a mistake in it is invisible from the outside.
"""

from __future__ import annotations
