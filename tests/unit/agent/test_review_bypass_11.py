"""Review finding 11: ReDoS - the e-mail and URL patterns are quadratic on a long run without a delimiter.

`engine.pii._EMAIL` starts with `[^\\s@<>()\\[\\],;:\\"]+@`: on a string with no `@` the greedy class runs to the end
of the run from *every* start position, and `egress._URL`'s second alternative
(`\\b(?:[a-z0-9\\-]+\\.)+[a-z]{2,24}/`) does the same on `a.a.a.a...` and `1-1-1-...`. 10,000 characters take
~0.5 s per pattern, 100,000 characters ~50 s (measured), and a cell or a header in a client's file can be that
long (a base64 blob, a pasted document). Reachable through the real code paths:

* `egress.scrub` / `assert_clean` on any uncut text - `Egress.unalias` and `person_text` run `scrub` over the
  whole real header of every aliased column on **every reply** (a header longer than 64 characters is aliased);
* `tools._masked` -> `engine.pii.redact_cells` on the whole cell before it is cut (`value_counts`,
  `inspect_column`, `find_values`, `sample_rows`, ...);
* `assert_clean(prompt)` runs before the `MAX_PROMPT_CHARS` check.

The budget below is generous (0.3 s for 8,000 characters; a linear scanner needs milliseconds).
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from engine.agent import egress
from engine.agent.tools import _masked

N = 8_000
BUDGET_SECONDS = 0.3
HOSTILE = {"letters": "a" * N, "digits and hyphens": "1-" * (N // 2), "labels and dots": "a." * (N // 2)}


def _seconds(fn: Callable[[], object]) -> float:
    """The best of three runs: a busy machine (pytest -n 4 beside other jobs) slows one run, rarely all three,
    while a backtracking scanner is slow on every run."""
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


@pytest.mark.parametrize("shape", ["letters", "digits and hyphens"])
def test_scrub_and_the_backstop_are_linear_on_a_long_run(shape: str) -> None:
    text = HOSTILE[shape]
    assert _seconds(lambda: egress.scrub(text)) < BUDGET_SECONDS
    assert _seconds(lambda: egress.assert_clean(text)) < BUDGET_SECONDS


@pytest.mark.parametrize("shape", list(HOSTILE))
def test_a_tool_masks_a_long_cell_in_linear_time(shape: str) -> None:
    assert _seconds(lambda: _masked(HOSTILE[shape], 60)) < BUDGET_SECONDS


def test_a_very_long_header_does_not_make_every_reply_slow() -> None:
    gate = egress.Egress.build(["h" * N, "amount"])
    assert _seconds(lambda: gate.person_text("column_1 is the long one")) < BUDGET_SECONDS
