"""The tool catalogue of the design §5.5, as data (Plan O M232, DEC-1802).

Every capability Data Doctor will have, one row each: its number, name, kind, mode, status and exposure. The
registry (`engine.doctor.registry`) holds the tools built so far; a registered tool must match its row here
(`tests/unit/doctor/test_registry.py`), so the registry cannot drift from the design. Rows are added to the
registry milestone by milestone; the catalogue itself changes only with the design.

* **kind** - `read`, `propose` or `ask`; None for the conversation loop (CONVERSE), which calls tools and is
  not one. `approve` marks a PROPOSE whose answer always needs the person's explicit yes (APPROVE-class).
* **status** - `existing` (a Plan G tool or loop action, moved in as it is), `wrapper` (the function exists and
  the registry wraps it), `new` (the logic does not exist yet).
* **exposure** - `aggregate` only for counts and verdicts (`what_can_this_power`, `check_readiness`); every
  other tool returns a cell, a row or a masked example and is `session-only` (the design §5.3).

The catalogue counts 82 tools in 83 rows: Ask your data (#78) is the conversation loop's read-only mode and is
counted once with it (#77). Rows #82 and #83 were added with the design in Plan O M236 (DEC-1806): M240's
`read_workbook_sheets` and `find_excel_serial_dates`, built in wave 1 before the design gave them a row. They are
numbered after #81 so that no other tool's number changes.
"""

from __future__ import annotations

from typing import Final, Literal, NamedTuple

__all__ = ["CATALOGUE", "CatalogueRow", "row"]

Kind = Literal["read", "propose", "ask"]
Mode = Literal["rules", "ai", "rules + ai", "person"]
Status = Literal["existing", "wrapper", "new"]


class CatalogueRow(NamedTuple):
    number: int
    name: str
    kind: Kind | None
    mode: Mode
    status: Status
    exposure: Literal["session-only", "aggregate"] = "session-only"
    approve: bool = False
    counted_with: int | None = None
    """For #78: the row it is counted with (the conversation loop)."""


_R: Final = CatalogueRow

CATALOGUE: Final[tuple[CatalogueRow, ...]] = (
    # A. Reading and intake
    _R(1, "read_file", "read", "rules", "wrapper"),
    _R(2, "list_sheets", "read", "rules", "wrapper"),
    _R(3, "propose_intake", "propose", "rules", "wrapper"),
    _R(4, "read_other_format", "read", "rules", "new"),
    _R(5, "check_parser_loss", "read", "rules", "new"),
    _R(6, "preview_connection", "read", "rules", "wrapper"),
    _R(7, "propose_import", "propose", "rules + ai", "wrapper"),
    _R(8, "check_refresh", "read", "rules", "wrapper"),
    _R(9, "preflight", "read", "rules", "wrapper"),
    _R(10, "diagnose_files", "read", "rules", "wrapper"),
    # B. Looking: Plan G's 18 look tools
    _R(11, "get_profile", "read", "rules", "existing"),
    _R(12, "inspect_column", "read", "rules", "existing"),
    _R(13, "find_format_issues", "read", "rules", "existing"),
    _R(14, "describe_placeholder_values", "read", "rules", "existing"),
    _R(15, "describe_outcome", "read", "rules", "existing"),
    _R(16, "describe_repeats", "read", "rules", "existing"),
    _R(17, "find_roles", "read", "rules", "existing"),
    _R(18, "draft_outcome_labels", "read", "rules", "existing"),
    _R(19, "check_data", "read", "rules", "existing"),
    _R(20, "sample_rows", "read", "rules", "existing"),
    _R(21, "value_counts", "read", "rules", "existing"),
    _R(22, "find_values", "read", "rules", "existing"),
    _R(23, "describe_numbers", "read", "rules", "existing"),
    _R(24, "describe_dates", "read", "rules", "existing"),
    _R(25, "compare_columns", "read", "rules", "existing"),
    _R(26, "describe_missing", "read", "rules", "existing"),
    _R(27, "rate_by", "read", "rules", "existing"),
    _R(28, "describe_duplicates", "read", "rules", "existing"),
    # C. Understanding
    _R(29, "find_file_role", "read", "rules", "wrapper"),
    _R(30, "read_headers", "read", "rules", "wrapper"),
    _R(31, "second_opinion", "propose", "ai", "wrapper"),
    _R(32, "advise", "read", "rules", "wrapper"),
    _R(33, "first_look", "read", "rules", "wrapper"),
    _R(34, "how_files_fit", "read", "rules", "wrapper"),
    _R(35, "load_goal_brief", "read", "rules", "new"),
    _R(36, "what_can_this_power", "read", "rules", "wrapper", exposure="aggregate"),
    _R(37, "enough_data", "read", "rules", "wrapper"),
    _R(38, "data_request", "read", "rules", "wrapper"),
    _R(39, "explain", "read", "ai", "new"),
    _R(40, "find_time_columns", "read", "rules", "wrapper"),
    # D. Roles, keys, mapping and the outcome
    _R(41, "propose_role", "propose", "rules + ai", "wrapper"),
    _R(42, "propose_key", "propose", "rules", "wrapper"),
    _R(43, "propose_mapping", "propose", "rules + ai", "wrapper"),
    _R(44, "propose_setting", "propose", "rules + ai", "existing"),
    _R(45, "define_outcome", "propose", "rules", "new", approve=True),
    _R(46, "map_outcome_values", "propose", "rules + ai", "new", approve=True),
    _R(47, "check_target_type", "read", "rules", "new"),
    # E. Fixing: the step vocabulary
    _R(48, "propose_step", "propose", "rules + ai", "existing"),
    _R(49, "edit_by_hand", "propose", "person", "existing"),
    _R(50, "propose_renamed_copy", "propose", "rules", "wrapper"),
    _R(51, "synonym_clusters", "read", "rules", "new"),
    _R(52, "impossible_values", "read", "rules", "new"),
    _R(53, "detect_units", "read", "rules", "new"),
    _R(54, "keep_latest_per_id", "propose", "rules", "new"),
    _R(55, "normalise_key", "propose", "rules", "new"),
    # F. Several files
    _R(56, "join_files", "propose", "rules", "new"),
    _R(57, "stack_files", "propose", "rules", "new"),
    _R(58, "build_dataset", "propose", "rules", "wrapper"),
    _R(59, "split_file", "propose", "rules", "new"),
    # G. Locale, currency and Indian formats
    _R(60, "detect_locale", "read", "rules", "new"),
    _R(61, "parse_indian_numbers", "propose", "rules", "new"),
    _R(62, "indian_booleans", "propose", "rules", "new"),
    _R(63, "detect_india_ids", "read", "rules", "new"),
    _R(64, "convert_currency", "propose", "rules", "new", approve=True),
    _R(65, "find_personal_data", "read", "rules", "wrapper"),
    # H. Safety and governance checks
    _R(66, "check_leaks", "read", "rules", "wrapper"),
    _R(67, "map_consent", "propose", "rules", "new", approve=True),
    _R(68, "check_uplift_data", "read", "rules", "wrapper"),
    # I. Readiness, memory and follow-up
    _R(69, "check_readiness", "read", "rules", "new", exposure="aggregate"),
    _R(70, "request_data", "ask", "rules + ai", "new"),
    _R(71, "recall_treatment", "read", "rules", "wrapper"),
    _R(72, "drift_check", "read", "rules", "new"),
    _R(73, "read_run_checks", "read", "rules", "wrapper"),
    # J. Asking
    _R(74, "ask_question", "ask", "rules + ai", "new"),
    _R(75, "express_doubt", "ask", "rules + ai", "new"),
    _R(76, "request_approval", "ask", "rules", "wrapper"),
    # K. The conversation (the loop that calls tools; not a tool kind)
    _R(77, "chat_turn", None, "ai", "existing"),
    _R(78, "ask_your_data", None, "ai", "existing", counted_with=77),
    # L. Time settings
    _R(79, "estimate_training_time", "read", "rules", "new"),
    _R(80, "propose_training_budget", "propose", "rules", "new"),
    _R(81, "propose_time_settings", "propose", "rules", "new"),
    # M. Added in Plan O M236 (DEC-1806): M240's two tools, built in wave 1 (§A reading, §E fixing)
    _R(82, "read_workbook_sheets", "read", "rules", "new"),
    _R(83, "find_excel_serial_dates", "read", "rules", "new"),
    # N. The Data Doctor's own SQL step (sandboxed, read-only)
    _R(84, "propose_sql_step", "propose", "rules + ai", "new"),
    # O. The agent looks at its own work: the first rows before and after the plan (masked)
    _R(85, "preview_working_copy", "read", "ai", "new"),
    # P. Behaviour features from the point-in-time library (Step 7)
    _R(86, "propose_features", "propose", "rules + ai", "new"),
    # Q. An amount as the outcome (a regression goal): the spend in the days after the snapshot
    _R(87, "propose_amount_outcome", "propose", "rules", "new"),
)
"""The design §5.5, row by row."""

_BY_NAME: Final[dict[str, CatalogueRow]] = {entry.name: entry for entry in CATALOGUE}


def row(name: str) -> CatalogueRow | None:
    """The catalogue row of `name`, or None."""
    return _BY_NAME.get(name)
