"""A configuration root that still carries a `planned` use case.

`configs/industries/telecom.yaml` had one — the AI Onboarding Assistant — until Phase 3a shipped
it and flipped the entry to `available`. Nothing in the shipped configuration is planned any more,
which left every test of the planned-entry rules with nothing to test: a planned entry carries its
own name and description because there is no use-case file to read them from, `GET /use-cases/{id}`
answers `404 USE_CASE_PLANNED` for one, and an upload or a run naming one is refused before a byte
is stored.

Those rules did not go away with the last planned use case, so they are proved against a fixture
instead, and they go on being proved whatever the shipped configuration comes to contain. The
fixture names a use case the plan really does defer (uplift modelling, plan section 12), so nobody
reading it has to wonder whether the id is a typo.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Final

from engine.config import DEFAULT_CONFIG_ROOT

__all__ = ["INDUSTRY_FIXTURE", "PLANNED_ID", "PLANNED_NAME", "planned_config_root"]

FIXTURES: Final[Path] = Path(__file__).resolve().parent / "configs"

INDUSTRY_FIXTURE: Final[str] = "industry_with_a_planned_use_case.yaml"
"""The industry file `planned_config_root` swaps in: valid, and carrying one planned entry."""

PLANNED_ID: Final[str] = "uplift-modelling"
"""The planned use case in that file. It has no `configs/use_cases/` entry, and must not gain one."""

PLANNED_NAME: Final[str] = "Uplift Modelling"


def planned_config_root(tmp_path: Path) -> Path:
    """A copy of `configs/` whose telecom industry file still has a planned use case.

    The whole directory is copied rather than assembled, so the root a test points an app at is the
    real configuration in every respect but the one line the test is about.
    """
    root = tmp_path / "configs"
    shutil.copytree(DEFAULT_CONFIG_ROOT, root)
    shutil.copy(FIXTURES / INDUSTRY_FIXTURE, root / "industries" / "telecom.yaml")
    return root
