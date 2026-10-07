"""Every error and check code Plan J can raise (DEC-1300 (d)).

Each milestone adds its codes here **and** to `configs/pilot/help.yaml` in the same change.
`engine.pilot.help.known_codes()` returns this set along with the engine's other codes, so a code
added here without a plain-language entry fails `tests/unit/pilot/test_help.py`, and a catalogue
entry for a code that is not raised anywhere fails the same test the other way round.
"""

from __future__ import annotations

from typing import Final

__all__ = ["PLAN_J_CODES"]

PLAN_J_CODES: Final[frozenset[str]] = frozenset(
    {
        # M91 (DEC-1301 (b)): `engine.privacy.config.load_privacy_config` refuses a contacting use case
        # with no consent purpose.
        "CONSENT_PURPOSE_MISSING",
        # M91 (DEC-1301 (e)): the audit reason code of a row-level download refused to a non-Analyst
        # (`api.routes.runs.require_row_level_role`); the HTTP error itself is `ROLE_REQUIRED`.
        "ROW_LEVEL_DOWNLOAD_REFUSED",
    }
)
"""Empty at M90; each Plan J milestone from M91 on adds the codes it raises."""
