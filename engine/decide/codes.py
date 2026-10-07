"""Every error and check code Plan J can raise (DEC-1300 (d)).

Each milestone adds its codes here **and** to `configs/pilot/help.yaml` in the same change.
`engine.pilot.help.known_codes()` returns this set along with the engine's other codes, so a code
added here without a plain-language entry fails `tests/unit/pilot/test_help.py`, and a catalogue
entry for a code that is not raised anywhere fails the same test the other way round.
"""

from __future__ import annotations

from typing import Final

__all__ = ["PLAN_J_CODES"]

PLAN_J_CODES: Final[frozenset[str]] = frozenset()
"""Empty at M90: Plan J adds codes from M91 (for example `CONSENT_PURPOSE_MISSING`) onwards."""
