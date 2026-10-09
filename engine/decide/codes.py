"""Every error and check code Plan J can raise (DEC-1300 (d)).

Each milestone adds its codes here **and** to `configs/pilot/help.yaml` in the same change.
`engine.pilot.help.known_codes()` returns this set along with the engine's other codes, so a code
added here without a plain-language entry fails `tests/unit/pilot/test_help.py`, and a catalogue
entry for a code that is not raised anywhere fails the same test the other way round.
"""

from __future__ import annotations

from typing import Final

from engine.decide.channel_columns import CHANNEL_COLUMN_CODES
from engine.decide.offer_run import OFFER_CHOICE_CODES
from engine.measurement.arms import MULTI_ARM_CODES
from engine.measurement.codes import MEASUREMENT_CHECK_CODES
from engine.model_gates import UPLIFT_GATE_CODES

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
            # M97 (DEC-1307 (i)): `GET /runs/{id}/uplift/profit-curve` given both `value` and
            # `value_per_conversion`, of which `value` is an alias (`api.routes.uplift`, 422). An API route
            # error, joined here so the catalogue explains it as it does M94's campaign route errors.
            "PROFIT_CURVE_QUERY_INVALID",
            # M98 (DEC-1308 (k), (o)): `GET /runs/{id}/treat_list.csv` and
            # `/runs/{id}/artefacts/treat_list.(csv|parquet|_summary.json)` answer 409 for a run that cannot have
            # a treat list (`engine.decide.treat_list.TreatListError`, mapped by `api.routes.runs.read_artefact`).
            # An existing route code of `api/routes/{uplift,measure,campaigns}.py`, joined here because the treat
            # list routes now raise it too, so the catalogue explains it as it does M97's route error.
            "RUN_NOT_SCORED",
            # M99 (DEC-1309 (c), (k)): the action catalogue's refusals, raised as `ConfigError` by
            # `engine.decide.catalogue` (catalogue load and `validate_action_ids`, called by `load_use_case` and
            # `resolve_config`) and by `engine.pilot.roi.lookup_value_costs` for an action id the catalogue lacks.
            "CATALOGUE_ACTION_UNKNOWN",
            "ACTION_DLT_TEMPLATE_MISSING",
            "CATALOGUE_INVALID",
            # M99 (DEC-1309 (j), (m)): a consent import row whose channel is not a channel name
            # (`engine.privacy.consent`, a per-row `ConsentImportError`). The other CONSENT_* row codes predate
            # Plan J and are not in the catalogue; this one is joined because it is new and user-facing.
            "CONSENT_CHANNEL_INVALID",
            # M101 (DEC-1311 (l), (p), (r), (v)): `POST /decide/arbitrate` and the arbitration downloads
            # (`api.routes.decide`, `engine.decide.arbitrate`). Route codes, joined here so the catalogue
            # explains them as it does M94's campaign route errors.
            "NO_RUNS_TO_ARBITRATE",  # 400: no finished scoring run of the use cases named
            "PRIMARY_KEY_MISMATCH",  # 422: the runs identify customers by different key columns
            "ARBITRATION_USE_CASE_REPEATED",  # 422: two runs of one use case
            "ARBITRATION_NOT_FOUND",  # 404: no arbitration yet, or no participating run keeps the list
            "ARBITRATION_CONFIG_INVALID",  # 422: decide/arbitration.yaml exists and cannot be read
        }
    )
    | MEASUREMENT_CHECK_CODES  # M93 (DEC-1303 (h)): LABEL_RATE_UNSTABLE, TREATMENT_HISTORY_NOT_RANDOM
    | UPLIFT_GATE_CODES  # M96 (DEC-1306 (f)): UPLIFT_NOT_BETTER_THAN_RISK, UPLIFT_UNSTABLE_ACROSS_FOLDS,
    # UPLIFT_MISCALIBRATED, the advisory approval checks (`engine.model_gates`)
    | MULTI_ARM_CODES  # M100 (DEC-1310 (h)): MULTI_ARM_PROMOTION_REFUSED, a model of several offers is never
    # champion, by its training run or by `POST /models/{id}/promote` (`engine.measurement.arms`)
    | OFFER_CHOICE_CODES  # M100 part B (DEC-1310 (r)): OFFER_CHOICE_NOT_MADE, recorded in offer_choice.json
    # when a run of several offers has no value to choose by (`engine.decide.offer_run`)
    | CHANNEL_COLUMN_CODES  # M100 part B (DEC-1310 (y)): CHANNEL_COLUMN_MODEL_INPUT, a scoring run refuses a
    # model trained with a channel's consent or contactable column as an input (`engine.decide.channel_columns`)
)
"""Empty at M90; each Plan J milestone from M91 on adds the codes it raises.

M93's two readiness warnings are defined once, in `engine.measurement.codes.MEASUREMENT_CHECK_CODES`,
and joined here, so `engine.contracts.ValidationCheck`'s PLAN-J hook and this set read the same
definition (the one-registry rule, DEC-950). M96's three advisory approval checks are joined the same
way from `engine.model_gates.UPLIFT_GATE_CODES` (DEC-1306 (f)), and M100's promotion refusal from
`engine.measurement.arms.MULTI_ARM_CODES` (DEC-1310 (h)). M100 part B's two codes are joined from
`engine.decide.offer_run.OFFER_CHOICE_CODES` and `engine.decide.channel_columns.CHANNEL_COLUMN_CODES`
(DEC-1310 (r), (y)). M101's five arbitration route codes are listed above (DEC-1311)."""
