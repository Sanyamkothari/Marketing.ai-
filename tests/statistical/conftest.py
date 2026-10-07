"""Keep the statistical suite out of every default run (Plan J M90, DEC-1300 (e)).

These tests simulate thousands of experiments to check that our intervals cover what they claim. They
are far too slow for `make test`, and `make test` / `make test-all` may not be changed to exclude them
(`PARALLEL_WORK_PROTOCOL.md` §4). So the exclusion lives here: unless `MARKETING_AI_STATISTICAL=1` is
set, pytest is told to ignore every `test_*.py` in this directory. They are then never collected, so
nothing is reported as skipped and `scripts/check_no_skips.py` has nothing to object to.

`make test-statistical` sets the variable and runs `-m statistical`; the nightly workflow calls that.
"""

from __future__ import annotations

import os

ENV_VAR = "MARKETING_AI_STATISTICAL"

collect_ignore_glob: list[str] = [] if os.environ.get(ENV_VAR) == "1" else ["test_*.py"]
