"""Every error and check code Plan J can raise (DEC-1300 (d)).

Each milestone adds its codes here **and** to `configs/pilot/help.yaml` in the same change.
`engine.pilot.help.known_codes()` returns this set along with the engine's other codes, so a code
added here without a plain-language entry fails `tests/unit/pilot/test_help.py`, and a catalogue
entry for a code that is not raised anywhere fails the same test the other way round.
"""

from __future__ import annotations

from typing import Final

from engine.measurement.codes import MEASUREMENT_CHECK_CODES

__all__ = ["PLAN_J_CODES"]

PLAN_J_CODES: Final[frozenset[str]] = (
    frozenset(
        {
            # M91 (DEC-1301 (b)): `engine.privacy.config.load_privacy_config` refuses a contacting use case
            # with no consent purpose.
            "CONSENT_PURPOSE_MISSING",
            # M91 (DEC-1301 (e)): the audit reason code of a row-level download refused to a non-Analyst
            # (`api.routes.runs.require_row_level_role`); the HTTP error itself is `ROLE_REQUIRED`.
            "ROW_LEVEL_DOWNLOAD_REFUSED",
            # M92 (DEC-1302 (a), (b)): the persistent holdout's refusals, raised as `HoldoutError` by
            # `engine.holdout.spec` / `engine.holdout.salt` (ingest pre-check and `PUT /holdout`).
            "HOLDOUT_SALT_MISSING",
            "HOLDOUT_SALT_CHANGED",
            "HOLDOUT_SALT_UNCHANGED",
            "HOLDOUT_FRACTION_LOWERED",
            "HOLDOUT_FRACTION_MISMATCH",
            "HOLDOUT_SCOPE_RESERVED",
            # M94 (DEC-1304): the campaign record, its one measurement path and its registered test plan
            # (`api.routes.campaigns`, `engine.measurement.campaign`, `engine.measurement.plan`).
            "CAMPAIGN_NOT_FOUND",
            "CAMPAIGN_NOT_MATURED",
            "CAMPAIGN_OUTCOMES_MISSING",
            "CAMPAIGN_INVALID",
            "TEST_PLAN_EXISTS",
            "TEST_PLAN_CHANGED",
            "TEST_PLAN_NOT_FOUND",
            "TEST_PLAN_INVALID",
            "PLAN_UNDERPOWERED",  # a warning carried by the plan, never a refusal
            # M94 integration (DEC-1304 (l)): measuring a campaign whose control group is not one
            # persistent holdout epoch's (`engine.measurement.campaign.epoch_mismatch`, M92's epochs).
            "CAMPAIGN_EPOCH_MISMATCH",
        }
    )
    | MEASUREMENT_CHECK_CODES
)  # M93 (DEC-1303 (h)): LABEL_RATE_UNSTABLE, TREATMENT_HISTORY_NOT_RANDOM
"""Empty at M90; each Plan J milestone from M91 on adds the codes it raises.

M93's two readiness warnings are defined once, in `engine.measurement.codes.MEASUREMENT_CHECK_CODES`,
and joined here, so `engine.contracts.ValidationCheck`'s PLAN-J hook and this set read the same
definition (the one-registry rule, DEC-950)."""
