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

PLAN_O_CODES: Final[frozenset[str]] = frozenset(
    {
        # M231 (DEC-1801): the case (`engine.doctor.case.CASE_CODES`).
        "CASE_NOT_FOUND",
        "CASE_READ_ONLY",
        "CASE_UNREADABLE",
        "CASE_VERSION_CONFLICT",
        # M234 (DEC-1804): the readiness report's fifteen probes (`engine.doctor.readiness_contracts.READINESS_CODES`;
        # X_PARSER_ROW_LOSS is also M240's parser check).
        "X_ALLOWED_FIELDS_FILLED",
        "X_CHANNEL_COLUMNS_PRESENT",
        "X_COMPLAINT_COLUMN_PRESENT",
        "X_CONSENT_VOCAB",
        "X_GROUPS_ENOUGH",
        "X_LABEL_MATURITY",
        "X_PARSER_ROW_LOSS",
        "X_PK_UNIQUE_FULL",
        "X_POST_FILTER_THRESHOLDS",
        "X_REGRESSION_TARGET_NUMERIC",
        "X_SPLIT_CLASSES",
        "X_TIME_ALL_PARSE",
        "X_TIME_CUTS_FEASIBLE",
        "X_TREATMENT_TOKENS",
        "X_VALUE_COLUMN_MISSING_SHARE",
        # M237 (DEC-1807): several files (`engine.doctor.join_files.JOIN_CODES`, `stack_files.STACK_CODES`,
        # `build_driver.BUILD_CODES`). APPROVAL_SENTENCE_CHANGED is the plan's shared exact-text code (M239, M246);
        # M237's `drive_builder` is the first to raise it.
        "APPROVAL_SENTENCE_CHANGED",
        "BUILD_DEFINES_OUTCOME",
        "BUILD_FILE_LEFT_OUT",
        "BUILD_PLAN_INCOMPLETE",
        "BUILD_PLAN_NOT_APPROVED",
        "BUILD_REPLAY_FILES",
        "BUILD_REWRITES_IDS",
        "BUILD_STEP_FAILED",
        "JOIN_COLUMN_CLASH",
        "JOIN_COLUMN_MISSING",
        "JOIN_DEFINES_OUTCOME",
        "JOIN_DROPS_ROWS",
        "JOIN_KEYS_DO_NOT_MEET",
        "JOIN_KEY_REPEATS",
        "STACK_COLUMN_MISSING",
        "STACK_CONFLICTS_UNDECIDED",
        "STACK_DROPS_ROWS",
        "STACK_TOO_FEW_FILES",
        # M238 (DEC-1808): row and value tools (`engine.doctor.tools_rows.ROW_TOOL_CODES`).
        "AI_STEP_CHANGES_MANY",
        "AI_STEP_CHANGES_TOO_MANY",
        "DOCTOR_COLUMN_PROTECTED",
        "DOCTOR_COLUMN_UNKNOWN",
        "IMPOSSIBLE_VALUE",
        "RESHAPE_GUARD_MISSING",
        "SPLIT_LATER_DATES_IN_TRAINING",
        "SPLIT_PART_EMPTY",
        "SPLIT_RULE_INVALID",
        "SPLIT_VALUES_UNREADABLE",
        "SYNONYM_MERGE_INVALID",
        "UNIT_MISMATCH",
        # M240 (DEC-1810): parser safety (`engine.doctor.parser_loss.PARSER_LOSS_CODES`, with X_PARSER_ROW_LOSS above).
        "X_PARSER_COLUMNS_SHIFTED",
        # M233 (DEC-1803): the data policy (`engine.doctor.policy`; POLICY_NO_RULES and POLICY_PAYLOAD_INVALID are
        # Plan K's).
        "POLICY_BOOLEAN_SPELLING",
        "POLICY_NEVER_TICKED",
        "POLICY_STEP_NOT_TRIED",
        # M235 (DEC-1805): the visit (`engine.doctor.visit`, `api/routes/case_visit.py`).
        "VISIT_NOT_WAITING",
        "VISIT_UNKNOWN_CHOICE",
        # M236 (DEC-1806): route codes the wrapper tools now raise too (`engine.doctor.tools_wrappers`;
        # `engine.connections.limits`; AGENTIC_DISABLED is Plan K's).
        "AGENT_SESSION_APPLIED",
        "CONNECTION_BUSY",
        "CONNECTION_NEEDS_ADDON",
        # M246 (DEC-1816): the protocol (`engine.doctor.protocol`, `engine.doctor.standing`; it also reuses
        # APPROVAL_SENTENCE_CHANGED above and Plan K's POLICY_ROLE_SEPARATION and LEDGER_CHAIN_BROKEN).
        "APPROVAL_CONFIRMATION_MISSING",
        "DATA_REQUESTED",
        "DIFFERENT_FILE_REQUESTED",
        "DOUBT_UNGROUNDED",
        "STANDING_ANSWER_NOT_FOUND",
        # M242 (DEC-1812): currency (`engine.doctor.currency`; CURRENCY_MIXED is also a recipe check code in
        # `engine.contracts.AGENT_VALIDATION_CODES`). M242's rate approval also raises APPROVAL_SENTENCE_CHANGED above.
        "CURRENCY_FOREIGN",
        "CURRENCY_MIXED",
        "CURRENCY_RATE_INVALID",
        "CURRENCY_RATE_UNAPPROVED",
        "CURRENCY_UNSTATED",
        # M239 (DEC-1809): the outcome tools (`engine.doctor.outcome_definition.OUTCOME_CODES`; the exact-text refusal
        # is APPROVAL_SENTENCE_CHANGED above, the approval's role refusals are Plan K's).
        "OUTCOME_CANNOT_MEASURE",
        "OUTCOME_NOT_APPROVED",
        "OUTCOME_TOO_MANY_VALUES",
        "OUTCOME_UNDECIDED",
        "OUTCOME_VALUE_NOT_MAPPED",
        # M244 (DEC-1814): time settings (`engine.doctor.time_settings.TIME_CODES`).
        "TIME_BUDGET_NOT_USED",
        "TIME_COLUMN_UNREADABLE",
        # M243 (DEC-1813): the prescription (`engine.doctor.prescription.PRESCRIPTION_CODES`; a replay also stops with
        # CURRENCY_FOREIGN, CURRENCY_MIXED and the OUTCOME_* codes above, or a step's own RECIPE_* or BUILD_* code).
        "PRESCRIPTION_DRIFT",
        "PRESCRIPTION_NOT_FOUND",
    }
)
"""Every code Plan O raises, grouped by milestone. M232 (the registry) reuses Plan K's TOOL_* codes, and M241 (locale),
M247 (the screen) and M248 (the evals; its EVAL_* codes are for developers only) raise none. Each code here has its
entry in `configs/pilot/help.yaml`."""
