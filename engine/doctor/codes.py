"""Every error and refusal code Plan O (Data Doctor, the one data agent) can raise (`PLAN_O_CODES`).

Plan O (`docs/plans/MARKETING_AI_PLAN_O_DATA_DOCTOR.md`, M230-M250) keeps one registry of its own codes, as Plan J
(`engine.decide.codes.PLAN_J_CODES`) and Plan K (`engine.agentic.codes.PLAN_K_CODES`) do. M230 (DEC-1800 (b)) wires
this set into `engine.pilot.help.known_codes()`, so from then on each milestone adds the codes it raises here **and**
their plain-language entries to `configs/pilot/help.yaml` in the same change: a code added here without an entry
fails `tests/unit/pilot/test_help.py`, and an entry for a code nothing raises fails the same test the other way round.

This module imports nothing, so `engine.pilot.help` can read it without loading the Data Doctor package.
"""

from __future__ import annotations

from typing import Final

__all__ = ["PLAN_O_CODES"]

PLAN_O_CODES: Final[frozenset[str]] = frozenset()
"""Empty at M230: the set-up raises nothing new. Each Plan O milestone adds the codes it raises, grouped by milestone
(for example M231's `CASE_VERSION_CONFLICT` and `CASE_NOT_FOUND`)."""
