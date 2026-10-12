"""Fail when README.md calls a milestone "pending" that the test suite says is built.

The README's milestone tables are the first thing a reader trusts and the last thing anybody
updates. On 23 Sep 2026 five Phase 2 milestones (M8 ... M12) still read "pending" a day after they
were merged with their tests green: the table was written when the branch opened and nothing ever
asked it again. Plan A M39 makes the question mechanical, the same way `test_docs_honesty.py` made
the deployment guide's cost tables mechanical: a claim in a document that rots silently is checked
rather than remembered.

How it decides, and why it is built this way:

* **The README is parsed, not grepped.** Every Markdown table with a `Status` column is read, and a
  row counts when its first cell is a milestone id (`M8`, `**M34**`). The word "pending" anywhere
  else - in prose, in a table about something that is not a milestone - is not a claim about a
  milestone and is left alone. A README with no milestone table at all is an error rather than a
  pass, because a parser that silently finds nothing is exactly the kind of green this check exists
  to stop.
* **The mapping from milestone to tests is explicit** (`MILESTONE_TESTS`, below). It is not
  inferred from test names or commit messages: a reviewer reads one dict to see what "M9 has passing
  tests" means, and a milestone that is not in it is never flagged, only reported. Every mapped path
  must exist, so a renamed test file fails here instead of quietly turning into "no evidence".
* **Evidence comes from a JUnit XML report, not a second test run.** CI's `make test` step already
  ran the fast suite; `ci.yml` asks it for `--junitxml` through `PYTEST_ADDOPTS` (the Makefile's
  `test` target is not ours to change) and this script reads that file, so the check costs a parse
  rather than minutes. Without `--junit` it runs pytest itself, but only over the tests of
  milestones the README calls pending - in the steady state, when the README is honest, that is
  nothing at all.
* **"Passing" means at least one mapped test passed and none failed.** A skipped test is not
  evidence either way, and neither is a test the run never selected: a milestone whose only tests
  are `@slow` is not judged by the fast suite. That makes the check one-sided on purpose - it can
  say a "pending" is false, never that a "done" is true.

Exit codes: 0 honest, 1 at least one milestone wrongly pending, 2 the check could not run (no
milestone table, a mapped path missing, an unreadable report, pytest crashed).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

COMMAND: Final[str] = "python -m scripts.check_readme"
REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_README: Final[str] = "README.md"

MILESTONE_TESTS: Final[Mapping[str, tuple[str, ...]]] = {
    # --- Phase 1 (plan.md §11) ---------------------------------------------------------------
    "M1": (
        "tests/unit/test_config_loading.py",
        "tests/unit/test_config_merge.py",
        "tests/unit/test_config_validators.py",
        "tests/unit/test_contracts.py",
        "tests/integration/test_api_config.py",
    ),
    "M2": (
        "tests/unit/test_ingest.py",
        "tests/unit/test_validate.py",
        "tests/integration/test_api_uploads.py",
        "tests/integration/test_api_runs.py",
    ),
    "M3": (
        "tests/unit/test_prepare.py",
        "tests/unit/test_split.py",
        "tests/unit/test_evaluate.py",
        "tests/unit/test_register.py",
        "tests/unit/test_run_train.py",
        "tests/integration/test_train_flow.py",
    ),
    "M4": (
        "tests/unit/test_drift.py",
        "tests/unit/test_actions.py",
        "tests/unit/test_export.py",
        "tests/unit/test_run_score.py",
        "tests/integration/test_score_flow.py",
    ),
    "M5": ("tests/integration/test_ui.py", "tests/integration/test_acceptance.py"),
    "M6": ("tests/unit/test_config_only_reuse.py",),
    "M7": ("tests/unit/test_logging_audit.py", "tests/unit/test_jobs.py"),
    # --- Phase 2 (onboarding) ----------------------------------------------------------------
    "M8": (
        "tests/unit/onboarding/test_clients.py",
        "tests/unit/onboarding/test_sources.py",
        "tests/unit/onboarding/test_roles.py",
        "tests/integration/test_api_clients.py",
    ),
    "M9": ("tests/unit/onboarding/test_mapping.py", "tests/unit/onboarding/test_transforms.py"),
    "M10": ("tests/unit/onboarding/test_features.py",),
    "M11": ("tests/unit/onboarding/test_labels.py", "tests/unit/onboarding/test_snapshots.py"),
    "M12": (
        "tests/unit/onboarding/test_datasets.py",
        "tests/unit/onboarding/test_validate.py",
        "tests/integration/test_api_datasets.py",
        "tests/integration/test_runs_from_dataset.py",
    ),
    # M13's definition of done is the panel *inside Setup*. The standalone panel's tests
    # (test_onboarding_ui.py, test_onboarding_flow.py) pass whether or not Setup mounts it, so M13 is
    # proved only by the tests Plan A M35 wrote when it mounted the panel - the same ones as M35.
    "M13": (
        "tests/unit/onboarding/test_setup_wiring_ui.py",
        "tests/integration/test_onboarding_acceptance.py",
    ),
    "M14": ("tests/fixtures/raw/test_make_raw.py",),
    # --- Phase 3a (generative) ---------------------------------------------------------------
    "M15": (
        "tests/unit/generative/test_budget.py",
        "tests/unit/generative/test_prompts.py",
        "tests/unit/generative/test_guardrails.py",
    ),
    "M16": (
        "tests/unit/generative/test_parsing.py",
        "tests/unit/generative/test_vectorstore.py",
        "tests/unit/generative/test_retrieval.py",
        "tests/unit/generative/test_index.py",
        "tests/unit/generative/test_assistant.py",
    ),
    "M17": ("tests/unit/generative/test_evaluation.py",),
    "M18": ("tests/unit/generative/test_root_cause.py",),
    "M19": ("tests/unit/generative/test_win_back.py",),
    "M20": ("tests/integration/test_api_generative.py",),
    # --- Phase 4a (aws) ----------------------------------------------------------------------
    "M21": (
        "tests/unit/test_settings.py",
        "tests/unit/test_aws_secrets.py",
        "tests/unit/test_storage_contract.py",
        "tests/unit/test_s3_storage.py",
    ),
    "M22": ("tests/unit/test_container_files.py", "tests/unit/test_job_entrypoint.py"),
    "M23": (
        "tests/unit/test_sagemaker_jobs.py",
        "tests/unit/test_runs_module.py",
        "tests/integration/test_jobs_as_sagemaker.py",
    ),
    "M24": (
        "tests/unit/test_postgres_metadata.py",
        "tests/unit/test_alembic_migrations.py",
        "tests/unit/test_run_index.py",
        "tests/unit/test_s3_registry.py",
    ),
    "M25": ("tests/infra/",),
    "M26": (
        "tests/unit/test_metrics.py",
        "tests/unit/test_gen_dashboard.py",
        "tests/unit/test_docs_honesty.py",
    ),
    # --- Plan A (Phase 2 completion) ---------------------------------------------------------
    # Each names the tests that milestone wrote: a mapping that points at an older test would call
    # the new milestone built before it was.
    "M34": ("tests/unit/test_composite_key.py", "tests/integration/test_periodic_flow.py"),
    "M35": (
        "tests/unit/onboarding/test_setup_wiring_ui.py",
        "tests/unit/onboarding/test_replay.py",
        "tests/unit/onboarding/test_default_client.py",
        "tests/integration/test_api_onboarding_replay.py",
        "tests/integration/test_onboarding_acceptance.py",
    ),
    "M36": (
        "tests/unit/test_pii.py",
        "tests/unit/test_column_names.py",
        "tests/unit/test_threshold_auto.py",
    ),
    "M37": (
        "tests/unit/onboarding/test_build_speed.py",
        "tests/integration/test_onboarding_build_reads.py",
        "tests/unit/test_fingerprint_parallel.py",
    ),
    "M38": (
        "tests/unit/test_inactive_settings.py",
        "tests/integration/test_inactive_settings_ui.py",
        "tests/unit/test_train_local_mirror.py",
        "tests/integration/test_ui_journey.py",
    ),
    "M39": ("tests/unit/test_check_readme.py",),
    # --- Phase 3b (uplift) -------------------------------------------------------------------
    # The files the Phase 3b README section runs for each milestone. M45 is documentation and the
    # Criteo run, which has no test; it is reported, never flagged.
    "M40": (
        "tests/unit/uplift/test_uplift_checks.py",
        "tests/unit/uplift/test_uplift_data.py",
        "tests/unit/uplift/test_uplift_config.py",
    ),
    "M41": (
        "tests/unit/uplift/test_metrics.py",
        "tests/unit/uplift/test_champion.py",
        "tests/unit/uplift/test_learners.py",
    ),
    "M42": (
        "tests/unit/uplift/test_segments.py",
        "tests/unit/uplift/test_policy.py",
        "tests/unit/uplift/test_uplift_actions.py",
        "tests/unit/uplift/test_uplift_explain.py",
    ),
    "M43": (
        "tests/unit/uplift/test_incrementality.py",
        "tests/unit/uplift/test_ope.py",
        "tests/integration/uplift/test_campaign_results_phase1.py",
        "tests/integration/uplift/test_uplift_api.py",
    ),
    "M44": ("tests/unit/uplift/test_uplift_ui.py",),
    # --- Plan D (hardening) ------------------------------------------------------------------
    # The tests each milestone wrote, as README's Plan D table lists them. M56's two files are the
    # skip check it added and the container-file test whose M56 cases pin node, `npm ci` and
    # REQUIRE_JSDOM in ci.yml (a failure there fails the evidence). M57 and M58 are unmapped on
    # purpose: what is left of M57 is a laptop measurement and a network-blocked Criteo run
    # (`tests/unit/test_bench_1m.py` proves only the tooling), and M58 is documentation and owner
    # actions. No test can prove either, so they are reported, never flagged.
    "M53": (
        "tests/unit/uplift/test_uplift_two_column_keys.py",
        "tests/integration/uplift/test_uplift_two_column_keys.py",
        "tests/unit/uplift/test_phase1_pages_uplift.py",
        "tests/unit/uplift/test_uplift_drift.py",
    ),
    "M54": (
        "tests/integration/production/test_login_rate_limit.py",
        "tests/unit/production/test_login_throttle.py",
        "tests/unit/production/test_privacy_config.py",
        "tests/integration/production/test_approvals.py",
        "tests/integration/production/test_approvals_ui_js.py",
        "tests/integration/production/test_privacy_erasure_api.py",
        "tests/unit/production/test_plan_d_migration.py",
    ),
    "M55": ("tests/unit/onboarding/test_leak_check.py", "tests/integration/test_onboarding_leak_check.py"),
    "M56": ("tests/unit/test_check_no_skips.py", "tests/unit/test_container_files.py"),
    # --- Plan J (docs/plans/MARKETING_AI_PLAN_J_PRODUCT.md); each milestone adds its own entry ---
    "M90": (
        "tests/unit/test_shared_file_markers.py",
        "tests/unit/decide/test_plan_j_codes.py",
        "tests/unit/decide/test_statistical_collection.py",
    ),
    "M91": (
        "tests/integration/uplift/test_uplift_consent.py",
        "tests/unit/production/test_privacy_purpose_coverage.py",
        "tests/unit/generative/test_sms_one_way.py",
        "tests/integration/test_api_generative_copy_identity.py",
        "tests/integration/decide/test_row_level_downloads.py",
    ),
    "M92": (
        "tests/unit/holdout/test_assign.py",
        "tests/unit/holdout/test_spec.py",
        "tests/unit/holdout/test_salt.py",
        "tests/unit/holdout/test_holdout_flow.py",
        "tests/integration/holdout/test_holdout_api.py",
        "tests/integration/holdout/test_holdout_uplift_flow.py",
    ),
    "M93": (
        "tests/unit/measurement/test_planner.py",
        "tests/unit/measurement/test_lapse_labels.py",
        "tests/unit/measurement/test_readiness_planning.py",
        "tests/unit/measurement/test_treatment_history.py",
        "tests/unit/measurement/test_neutral_defaults.py",
        "tests/unit/measurement/test_codes.py",
        "tests/integration/measurement/test_grace_window_leak.py",
        "tests/integration/measurement/test_power_preview_route.py",
        "tests/integration/measurement/test_readiness_measure.py",
    ),
    "M94": (
        "tests/unit/measurement/test_campaign.py",
        "tests/unit/measurement/test_campaign_migration.py",
        "tests/unit/measurement/test_campaign_privacy.py",
        "tests/unit/measurement/test_measure.py",
        "tests/unit/measurement/test_plan.py",
        "tests/unit/measurement/test_roi_campaign.py",
        "tests/unit/measurement/test_campaign_epochs.py",
        "tests/integration/measurement/test_campaigns_api.py",
        "tests/integration/measurement/test_campaign_access.py",
        "tests/integration/measurement/test_campaign_direction.py",
        "tests/integration/measurement/test_campaign_erasure.py",
        "tests/integration/measurement/test_campaign_ui.py",
        "tests/integration/measurement/test_campaign_epochs.py",
        "tests/integration/measurement/test_plan_preview.py",
        "tests/integration/measurement/test_campaign_explore.py",
    ),
    # M95's nightly statistical tests (tests/statistical/) are left out: the fallback run selects the
    # fast suite, and a statistical file named on the command line would be collected (DEC-1300 (e)).
    "M95": (
        "tests/unit/measurement/test_simulate.py",
        "tests/unit/pilot/test_synthetic_quarantine.py",
        "tests/unit/test_docs_honesty.py",
        "tests/unit/production/test_firing.py",
        "tests/integration/test_synthetic_runs.py",
        "tests/integration/uplift/test_measure_campaign.py",
        "tests/integration/pilot/test_pilot_acceptance.py",
    ),
    # M96's nightly coverage test (tests/statistical/test_risk_comparison_coverage.py, DEC-1300 (e)) and its
    # node tests (ranking_banner.test.mjs, which test_ranking_banner_js.py runs, and the approvals
    # checks.test.mjs) are left out: this map names pytest files only.
    "M96": (
        "tests/unit/uplift/test_beats_risk.py",
        "tests/unit/uplift/test_dr_effect_terms.py",
        "tests/unit/measurement/test_compare.py",
        "tests/unit/decide/test_model_gates.py",
        "tests/unit/decide/test_ranking_choice.py",
        "tests/unit/decide/test_ranking_banner_js.py",
        "tests/integration/decide/test_uplift_ranking_flow.py",
    ),
    # M97's node tests (uplift_ui.test.mjs) run through tests/unit/uplift/test_uplift_ui.py, named here.
    "M97": (
        "tests/unit/uplift/test_net_value.py",
        "tests/unit/uplift/test_profit_curve.py",
        "tests/unit/uplift/test_uplift_ui.py",
        "tests/unit/decide/test_gross_value.py",
        "tests/unit/pilot/test_roi.py",
        "tests/integration/uplift/test_net_value_flow.py",
    ),
    # M98's node tests (treat_list_card.test.mjs, treat_list_page.test.mjs) run through
    # tests/unit/decide/test_treat_list_card_js.py, named here. test_treat_list_real_propensity.py is wholly
    # `slow`, so the fast-suite selection would collect nothing from it, and is left out.
    "M98": (
        "tests/unit/decide/test_treat_list.py",
        "tests/unit/decide/test_treat_list_scale.py",
        "tests/unit/decide/test_treat_list_card_js.py",
        "tests/unit/decide/test_m98_handoff.py",
        "tests/unit/holdout/test_selection_masks.py",
        "tests/unit/agent/test_reason_wording.py",
        "tests/integration/decide/test_treat_list_flow.py",
        "tests/integration/decide/test_treat_list_golden.py",
        "tests/integration/decide/test_treat_list_review.py",
        "tests/integration/decide/test_treat_list_real_run.py",
    ),
    # M99: test_channel_consent_real_propensity.py is wholly `slow`, so the fast-suite selection would collect
    # nothing from it, and is left out.
    "M99": (
        "tests/unit/decide/test_catalogue.py",
        "tests/unit/decide/test_contactability.py",
        "tests/unit/decide/test_m99_handoff.py",
        "tests/unit/production/test_consent_channel_migration.py",
        "tests/integration/decide/test_catalogue_consent.py",
        "tests/integration/decide/test_channel_consent_real_run.py",
    ),
    # M100 is mapped by both parts' tests (DEC-1310). The node tests (arms_card.test.mjs,
    # offer_choice_card.test.mjs) run through test_arms_card_js.py and test_offer_choice_card_js.py, and the
    # nightly coverage test (tests/statistical/test_arm_interval_coverage.py, DEC-1300 (e)) is left out, as
    # for M95 and M96.
    "M100": (
        "tests/unit/decide/test_multi_arm_model.py",
        "tests/unit/decide/test_offer_choice.py",
        "tests/unit/decide/test_offer_choice_scale.py",
        "tests/unit/decide/test_arms_card_js.py",
        "tests/unit/measurement/test_arms.py",
        "tests/integration/decide/test_multi_arm_flow.py",
        "tests/integration/decide/test_m100_binary_identity.py",
        "tests/unit/decide/test_offer_run.py",
        "tests/unit/decide/test_offer_run_scale.py",
        "tests/unit/decide/test_channel_gaps.py",
        "tests/unit/decide/test_scheduled_catalogue_stamp.py",
        "tests/unit/decide/test_offer_choice_card_js.py",
        "tests/integration/decide/test_offer_choice_run.py",
        "tests/integration/decide/test_offer_choice_explore.py",
        "tests/integration/decide/test_offer_choice_binary.py",
        "tests/integration/decide/test_channel_gate_real_run.py",
    ),
    # M101 (DEC-1311). The node test (conflicts_card.test.mjs) runs through test_conflicts_card_js.py; the
    # 200,000-row timing tests in test_arbitrate.py and test_arbitrate_scale.py are `slow`, and each file
    # keeps fast tests, so both stay mapped.
    "M101": (
        "tests/unit/decide/test_arbitrate.py",
        "tests/unit/decide/test_arbitrate_real_runs.py",
        "tests/unit/decide/test_arbitrate_scale.py",
        "tests/unit/decide/test_arbitrate_second_review.py",
        "tests/unit/decide/test_arbitrate_control_group.py",
        "tests/unit/decide/test_conflicts_card_js.py",
        "tests/integration/decide/test_arbitration_flow.py",
        "tests/integration/decide/test_arbitration_review.py",
        "tests/integration/decide/test_arbitration_second_review.py",
        "tests/integration/decide/test_control_group_arbitration.py",
    ),
    # M102 (DEC-1312). The nightly coverage test (tests/statistical/test_continuous_coverage.py, DEC-1300 (e))
    # is left out, as for M95, M96 and M100; m102_binary_golden.py is the golden's helper, not a test module.
    # test_amounts_scale.py keeps a fast linearity test beside its `slow` 1,000,000-row one, so it stays mapped.
    "M102": (
        "tests/unit/measurement/test_continuous.py",
        "tests/unit/measurement/test_planner_continuous.py",
        "tests/unit/measurement/test_roi_amount.py",
        "tests/unit/measurement/test_amounts_scale.py",
        "tests/unit/measurement/test_m102_binary_identity.py",
        "tests/integration/measurement/test_campaign_amounts.py",
    ),
    # M103 (DEC-1313). The nightly coverage test (tests/statistical/test_complier_coverage.py, DEC-1300 (e)) is
    # left out, as for M95, M96, M100 and M102. The node test (audit_view.test.mjs) runs through
    # test_audit_view.py. test_audit_scaling.py keeps a fast test beside its `slow` one, so it stays mapped.
    # test_campaign_privacy.py and test_campaign_access.py predate M103 (M94) and are not evidence for it.
    "M103": (
        "tests/unit/measurement/test_audit.py",
        "tests/unit/measurement/test_reconcile.py",
        "tests/unit/measurement/test_programme.py",
        "tests/unit/measurement/test_audit_scaling.py",
        "tests/unit/measurement/test_audit_view.py",
        "tests/integration/measurement/test_campaign_audit.py",
        "tests/integration/measurement/test_campaign_contacts.py",
        "tests/integration/measurement/test_programme_readout.py",
        "tests/integration/measurement/test_audit_trail.py",
    ),
    # M104 (DEC-1314). The nightly false-alarm test (tests/statistical/test_backfire_false_alarm.py, DEC-1300 (e))
    # is left out, as for M102 and M103. The node test (proof.test.mjs) runs through
    # tests/integration/simple/test_simple_ui.py, which predates M104 (Plan H) and is not evidence for it, as
    # test_pilot_ui.py is not. test_proof.py keeps fast tests beside its `slow` one-million-customer test.
    # The M104 fix (DEC-1314 (r)-(x)), a Pack of several offers: test_arms.py (M100's) gained the pooled
    # `combined_offers` tests the fix rests on; its nightly coverage test
    # (tests/statistical/test_offers_combined_coverage.py) is left out, as above.
    "M104": (
        "tests/unit/pilot/test_proof.py",
        "tests/unit/measurement/test_segments.py",
        "tests/integration/pilot/test_proof_pack.py",
        "tests/integration/pilot/test_proof_pack_programme.py",
        "tests/integration/pilot/test_proof_pack_offers.py",
        "tests/integration/pilot/test_proof_pack_amounts.py",
        "tests/integration/pilot/test_proof_pack_several_offers.py",
        "tests/unit/pilot/test_proof_single_offer_golden.py",
        "tests/unit/measurement/test_arms.py",
    ),
    # M105 (DEC-1315). The nightly false-alarm test (tests/statistical/test_fading_false_alarm.py, DEC-1300 (e)) is
    # left out, as for M102 to M104. The node test (summary.test.mjs) runs through
    # tests/integration/simple/test_simple_ui.py, which predates M105 (Plan H) and is not evidence for it;
    # test_campaign_access.py (M94) and test_api_config.py (trunk) gained one route each and are not evidence either.
    "M105": (
        "tests/unit/measurement/test_summary.py",
        "tests/integration/measurement/test_campaign_summary.py",
        "tests/integration/measurement/test_campaign_summary_cycles.py",
        "tests/integration/measurement/test_campaign_summary_units.py",
        "tests/integration/measurement/test_campaign_summary_scale.py",
    ),
    # M106 (DEC-1316). The node test (live_calibration.test.mjs) runs through
    # tests/integration/production/test_approvals_ui_js.py, which predates M106 (Phase 4b) and is not evidence for it.
    # test_uplift_run_cost.py is M108's request to M106, done at M106's integration (DEC-1316 (p)).
    "M106": (
        "tests/unit/measurement/test_learn.py",
        "tests/integration/measurement/test_learn_from_cycle.py",
        "tests/integration/measurement/test_learn_default_unchanged.py",
        "tests/integration/uplift/test_uplift_dataset_run.py",
        "tests/integration/decide/test_uplift_run_cost.py",
    ),
    # M107 (DEC-1317). test_monthly_loop.py is marked slow (the scheduled cycle on fakes, the Phase 4 gate's evidence).
    # test_api_config.py (trunk) gained two paths and test_scheduled_run_cost.py is M108's; neither is evidence for it.
    "M107": (
        "tests/unit/measurement/test_cycle_schedules.py",
        "tests/unit/measurement/test_cycle_upload.py",
        "tests/unit/measurement/test_outcome_pull.py",
        "tests/integration/measurement/test_bound_sources.py",
        "tests/integration/measurement/test_connection_pulls_api.py",
        "tests/integration/measurement/test_monthly_loop.py",
    ),
    # M108 (DEC-1318). The node test (cost_card.test.mjs) runs through test_cost_card_js.py.
    # test_api_config.py and test_ui.py (trunk) gained one line each and are not evidence for it.
    "M108": (
        "tests/unit/decide/test_run_cost.py",
        "tests/unit/decide/test_scheduled_run_cost.py",
        "tests/unit/decide/test_cost_card_js.py",
        "tests/integration/decide/test_run_cost_api.py",
        "tests/integration/decide/test_run_cost_defaults.py",
    ),
    # M109 (DEC-1319). The node tests (drift_events_card.test.mjs, rca_drift_events.test.mjs, planner_words.test.mjs)
    # run through test_drift_events_card_js.py, test_rca_drift_events_js.py and test_planner_words_js.py.
    # library/tests/test_online_retail.py and tests/unit/agent/test_advisor.py (the agent-bench golden) predate M109
    # and are not evidence for it.
    "M109": (
        "tests/integration/decide/test_drift_events.py",
        "tests/unit/agent/test_holdout_advice.py",
        "tests/unit/decide/test_verdict_wording.py",
        "tests/unit/decide/test_drift_events_card_js.py",
        "tests/unit/decide/test_rca_drift_events_js.py",
        "tests/unit/decide/test_planner_words_js.py",
    ),
    # M110 (DEC-1320). The library tests are opt-in (DEC-409) and outside `testpaths`, so the fast suite never
    # selects them and this mapping can only fail on a missing path; they run with `pytest library/tests/...`
    # (the full-file reproduction skips, saying why, when the git-ignored data is absent).
    # tests/unit/test_prototype_parity.py (trunk) gained one PER_USE_CASE entry and is not evidence for it.
    "M110": ("library/tests/test_hillstrom_email.py",),
    # M110a (DEC-1322), the audit readout on Hillstrom's own e-mail test that closes the Phase 3 gate's last item.
    # Opt-in library tests, like M110's: the fast suite never selects them; the full-file reproduction skips, saying
    # why, when the git-ignored data is absent. test_hillstrom_email.py checks the report the audit section joins.
    "M110a": (
        "library/tests/test_hillstrom_audit.py",
        "library/tests/test_hillstrom_email.py",
    ),
    # M111 (DEC-1321). The rehearsal by a person outside the team is pending and no test can prove it; these prove the
    # script, the generated summary and the seed. tests/unit/test_docs_honesty.py (trunk) is not evidence for it.
    "M111": (
        "tests/unit/test_demo_script.py",
        "tests/unit/test_demo_summary.py",
        "tests/unit/test_seed_validated.py",
    ),
    # M131 (DEC-1323), a Plan K milestone built early under Plan J's range: TRAI Third Amendment consent rules and
    # the measurement purpose. test_connection_pulls_api.py (M107's) and test_privacy_routes.py (trunk) gained its
    # Parquet import and purpose tests; test_consent_basis.py keeps a `slow` 200k-row test beside its fast ones.
    "M131": (
        "tests/unit/decide/test_a2p_declaration.py",
        "tests/unit/production/test_consent_basis.py",
        "tests/integration/decide/test_consent_basis_real_run.py",
        "tests/integration/measurement/test_connection_pulls_api.py",
        "tests/integration/production/test_privacy_routes.py",
    ),
    # M135 (DEC-1324), a Plan K milestone built early under Plan J's range: the scoring run's prediction as the
    # adjustment (CUPAC). The nightly test (tests/statistical/test_run_score_coverage.py, DEC-1300 (e)) is left out,
    # as for M96 and M137.
    "M135": (
        "tests/unit/measurement/test_run_score.py",
        "tests/unit/measurement/test_run_score_scale.py",
        "tests/integration/measurement/test_campaign_run_score.py",
    ),
    # M137 (DEC-1325), a Plan K milestone built early under Plan J's range: "safe to roll out". The nightly test
    # (tests/statistical/test_rollout_verdict.py, DEC-1300 (e)) is left out, as for M96. test_compare.py and
    # test_model_gates.py are M96's files and gained the verdict's tests; test_compare.py keeps a fast test beside its
    # `slow` one-million-row test.
    "M137": (
        "tests/unit/measurement/test_compare.py",
        "tests/unit/decide/test_model_gates.py",
        "tests/unit/uplift/test_uplift_config.py",
        "tests/integration/uplift/test_rollout_verdict_flow.py",
    ),
    # --- Plan L (docs/plans/MARKETING_AI_PLAN_L_DATA_DOCTOR.md); each milestone adds its own entry ---
    "M150": (
        "tests/unit/intake/test_grid.py",
        "tests/unit/intake/test_intake_bench.py",
        "tests/unit/intake/test_intake_run.py",
        "tests/unit/intake/test_intake_config.py",
        "tests/unit/intake/test_intake_connectors.py",
        "tests/integration/test_api_intake.py",
        "tests/integration/test_intake_ui.py",
    ),
    "M151": ("tests/unit/doctor/test_diagnose.py", "tests/integration/test_api_doctor.py"),
    "M152": ("tests/unit/agent/test_propose_step.py",),
    "M154": ("tests/unit/agent/test_edits.py", "tests/integration/agent/test_edit_ui.py"),
    "M153": (
        "tests/unit/agent/test_value_steps.py",
        "tests/unit/agent/test_recipe_plan_l.py",
        "tests/unit/agent/test_range_advice.py",
    ),
    "M155": (
        "tests/unit/doctor/test_joins.py",
        "tests/integration/test_api_doctor_joins.py",
        "tests/integration/agent/test_dataset_recipe_refusal.py",
    ),
    "M156": (
        "tests/unit/agent/test_unpivot.py",
        "tests/unit/agent/test_advisor_unpivot.py",
        "tests/integration/agent/test_unpivot_api.py",
    ),
    "M157": ("tests/unit/doctor/test_outcomes.py", "tests/integration/test_api_doctor_outcome.py"),
    "M158": ("tests/unit/doctor/test_sufficiency.py", "tests/integration/test_api_doctor.py"),
    "M159": ("tests/unit/doctor/test_memory.py", "tests/integration/agent/test_api_doctor_memory.py"),
    "M160": ("tests/integration/test_plan_l_benchmark.py",),
    "M161": ("tests/integration/test_intake_erasure.py", "tests/integration/test_doctor_ui.py"),
    # --- Plan K (docs/plans/MARKETING_AI_PLAN_K_PROPOSAL.md, docs/AGENTIC.md); each milestone adds its own entry.
    # M131, M135 and M137 were built early under Plan J's range; their entries are above, with Plan J's, and their
    # README rows are in the Plan K table. ---
    # M130 (DEC-1400), the set-up: the PLAN-K blocks, PLAN_K_CODES in the catalogue check, the three switches.
    "M130": (
        "tests/unit/test_shared_file_markers.py",
        "tests/unit/pilot/test_help.py",
        "tests/unit/agentic/test_flags.py",
    ),
    # M132 (DEC-1401), the agent-safe core. tests/unit/measurement/test_campaign_migration.py is Plan J's file; M132
    # made it pin the platform tables' order (agentic_decision_log after campaign).
    "M132": (
        "tests/unit/agentic/test_canonical.py",
        "tests/unit/agentic/test_ledger.py",
        "tests/unit/agentic/test_policy.py",
        "tests/unit/agentic/test_registry.py",
        "tests/unit/agentic/test_switch.py",
        "tests/agentic/scenarios/test_scenarios.py",
        "tests/agentic/test_proposal_routes.py",
        "tests/agentic/test_read_tools.py",
        "tests/unit/measurement/test_campaign_migration.py",
    ),
    # M136 (DEC-1402), pooling across comparable campaigns. The nightly coverage test
    # (tests/statistical/test_pooling_coverage.py, DEC-1300 (e)) is left out, as for M137. test_cycle_schedules.py is
    # M107's file and gained the test that the measure firing hands the config root to pooling.
    "M136": (
        "tests/unit/measurement/test_pooling.py",
        "tests/unit/measurement/test_pooling_summary.py",
        "tests/unit/measurement/test_cycle_schedules.py",
        "tests/integration/measurement/test_campaign_pooling.py",
    ),
    # M138 (DEC-1403), the early harm alarm. The nightly A/A and power test (tests/statistical/test_harm_alarm.py,
    # DEC-1300 (e)) is left out, as for M137.
    "M138": (
        "tests/unit/measurement/test_harm_alarm.py",
        "tests/integration/measurement/test_harm_alarm_loop.py",
    ),
    # M142 (DEC-1409), setup help for new sources. test_advisor.py is Plan G's benchmark test and pins M142's cases
    # in expected.json; test_header_switch.py pins the agentic.enabled gate added at integration.
    "M142": (
        "tests/unit/agent/test_headers.py",
        "tests/unit/agent/test_header_advice.py",
        "tests/unit/agent/test_header_benchmark.py",
        "tests/unit/agent/test_header_switch.py",
        "tests/unit/agent/test_assist.py",
        "tests/unit/agent/test_advisor.py",
        "tests/integration/agent/test_assist_api.py",
    ),
    # M133 (DEC-1404), the verifier over MCP on demo data.
    "M133": (
        "tests/unit/mcp/",
        "tests/integration/mcp/test_scripted_client.py",
        "tests/integration/mcp/test_threats.py",
        "tests/integration/mcp/test_small_groups.py",
        "tests/integration/mcp/test_injection_canaries.py",
    ),
    # M144 (DEC-1411), the save-desk card as a randomised pilot design. test_proposal_routes.py is M132's exact
    # /agentic route set, which gained the card's route.
    "M144": (
        "tests/unit/agentic/test_save_desk.py",
        "tests/unit/agentic/test_save_desk_pilot.py",
        "tests/agentic/test_save_desk.py",
        "tests/agentic/test_proposal_routes.py",
    ),
    # M139 (DEC-1406), the campaign planner and the claim verifier. test_planner_hillstrom.py is a `slow` journey and
    # is left out, as the fast suite leaves it out; test_proposal_routes.py is M132's exact route set, which gained
    # the planner's routes.
    "M139": (
        "tests/unit/agentic/test_claims.py",
        "tests/unit/agentic/test_planner.py",
        "tests/agentic/test_planner_routes.py",
        "tests/agentic/test_planner_select.py",
        "tests/agentic/test_planner_ui.py",
        "tests/agentic/scenarios/test_planner_scenarios.py",
        "tests/agentic/test_proposal_routes.py",
    ),
    # M134 (DEC-1405), the pre-send compliance gate. test_gate_scale.py's two 1M tests are `slow` (the fast suite
    # runs its 100k test); test_treat_list_golden.py is Plan J M98's golden, unchanged, which proves the gate off
    # leaves every treat list byte-identical.
    "M134": (
        "tests/unit/compliance/test_compliance_config.py",
        "tests/unit/compliance/test_dlt.py",
        "tests/unit/compliance/test_urls.py",
        "tests/unit/compliance/test_gate_rules.py",
        "tests/unit/compliance/test_gate_offer_arms.py",
        "tests/unit/compliance/test_gate_scale.py",
        "tests/integration/compliance/test_gate_real_run.py",
        "tests/integration/compliance/test_gate_offer_choice_run.py",
        "tests/integration/decide/test_treat_list_golden.py",
    ),
    # M140 (DEC-1407), message variants as measured arms. The nightly test (tests/statistical/test_variant_arms.py,
    # DEC-1300 (e)) is left out, as for M137; test_api_config.py is trunk's OpenAPI path set, which gained the route.
    "M140": (
        "tests/unit/decide/test_variant_arms.py",
        "tests/integration/decide/test_variant_arms_journey.py",
        "tests/integration/test_api_config.py",
    ),
    # M143 (DEC-1410), the model licence guard and the opt-in foundation strategy (moved from tests/unit/ at
    # integration). Its CPU-fit test is `slow` and runs only under -m slow.
    "M143": ("tests/unit/agentic/test_model_licences.py",),
    # M141 (DEC-1408), the verifier on client data: client workspaces, one ledger chain per workspace, receipts,
    # MCP tasks and the check_before_contact read tool.
    "M141": (
        "tests/unit/mcp/test_workspaces.py",
        "tests/unit/mcp/test_tasks.py",
        "tests/unit/mcp/test_suppress.py",
        "tests/unit/agentic/test_workspace_chains.py",
        "tests/integration/mcp/test_client_workspaces.py",
        "tests/integration/compliance/test_check_before_contact.py",
    ),
    # M145 (DEC-1412), message variants pass the compliance gate (a follow-up found at the integration of M134 and
    # M140). test_gate_variants_scale.py's 1M test is `slow` (the fast suite runs its 100k test).
    "M145": (
        "tests/unit/compliance/test_gate_variants.py",
        "tests/unit/compliance/test_gate_variants_scale.py",
        "tests/integration/compliance/test_gate_variant_arms.py",
        "tests/unit/decide/test_variant_arms.py",
    ),
    # --- Plan M (docs/plans/MARKETING_AI_PLAN_M_SIMPLE_UI.md); each milestone adds its own entry. ---
    # M170 (DEC-1600), the set-up, the glossary and the visible-text check. test_visible_text_ui.py runs the node
    # suites (labels.test.mjs, visible_text.test.mjs) when jsdom is installed and checks the recorded answers and the
    # baseline without it; test_ui.py registers ui/labels.js; test_shared_file_markers.py pins the PLAN-M blocks.
    "M170": (
        "tests/integration/test_visible_text_ui.py",
        "tests/integration/test_ui.py",
        "tests/unit/test_shared_file_markers.py",
    ),
    # M171 (DEC-1601), one contact list: one "Download contact list" (the treat list), the sentence built from the
    # run's own files, a shorter Output page. test_contact_list_js.py runs contact_list.test.mjs and
    # contact_list_api.test.mjs; test_uplift_ui.py runs uplift_ui.test.mjs (the uplift sentence and download).
    "M171": (
        "tests/integration/decide/test_one_contact_list.py",
        "tests/unit/decide/test_contact_list_js.py",
        "tests/unit/uplift/test_uplift_ui.py",
        "tests/integration/decide/test_treat_list_review.py",
        "tests/integration/production/test_production_ui.py",
    ),
    # M172 (DEC-1602), words and numbers. test_visible_text_ui.py draws every page with an empty baseline and runs
    # m172_labels.test.mjs and words_numbers.test.mjs; test_format_js.py runs format.test.mjs (ui/format.js); the
    # simple, measurement, reports and uplift runners pin the reworded pages; test_acceptance.py and
    # test_uplift_browser.py are the Playwright journeys whose words changed.
    "M172": (
        "tests/integration/test_visible_text_ui.py",
        "tests/unit/ux/test_format_js.py",
        "tests/integration/simple/test_simple_ui.py",
        "tests/integration/measurement/test_campaign_ui.py",
        "tests/integration/test_reports_ui.py",
        "tests/unit/uplift/test_uplift_ui.py",
        "tests/integration/test_acceptance.py",
        "tests/integration/uplift/test_uplift_browser.py",
    ),
    # M173 (DEC-1603), Help and loose ends. test_ui_sweep.py is the Playwright route sweep (zero console errors), the
    # click crawler and the Help menu by keyboard; test_simple_ui.py runs help.test.mjs and settings.test.mjs (the
    # menu, the tour, the orphans, #/industry, readiness by goal); test_production_ops_ui_js.py runs monitoring.test.mjs
    # (the value-view shortcut); test_production_ui_js.py runs topbar.test.mjs and crumbs.test.mjs; test_uplift_ui.py
    # runs uplift_ui_wiring.test.mjs (the OPE read and the propensity-ranked budget curve).
    "M173": (
        "tests/integration/test_ui_sweep.py",
        "tests/integration/simple/test_simple_ui.py",
        "tests/integration/production/test_production_ops_ui_js.py",
        "tests/integration/production/test_production_ui_js.py",
        "tests/unit/uplift/test_uplift_ui.py",
        "tests/integration/test_ui_journey.py",
    ),
    # M174 (DEC-1604), the new shell. test_shell_ui.py runs shell.test.mjs, routes.test.mjs (every registered route
    # has a home) and classic.test.mjs (the classic bar is byte for byte the same, checked differentially);
    # test_shell_browser.py is Playwright in both layouts; test_ui_layout_setting.py the setting and /auth/me field.
    "M174": (
        "tests/integration/test_shell_ui.py",
        "tests/integration/test_shell_browser.py",
        "tests/integration/production/test_ui_layout_setting.py",
        "tests/integration/test_visible_text_ui.py",
        "tests/integration/test_ui.py",
    ),
    # M175 (DEC-1605), the Overview and GET /overview. test_campaign_state_cases.py runs the one status rule over the
    # case table M176's rules.test.mjs runs too.
    "M175": (
        "tests/integration/overview/test_overview_api.py",
        "tests/integration/overview/test_overview_fixture.py",
        "tests/integration/test_overview_ui.py",
        "tests/unit/measurement/test_campaign_state_cases.py",
        "tests/integration/test_visible_text_ui.py",
    ),
    # M176 (DEC-1606), the Campaigns list and the campaign page. test_campaigns_page_ui.py runs rules.test.mjs,
    # demo.test.mjs and results.test.mjs (results added from the tab against a real API).
    "M176": (
        "tests/integration/test_campaigns_page_ui.py",
        "tests/integration/test_visible_text_ui.py",
    ),
    # M177 (DEC-1607), the New campaign wizard (as M181 part C changed it). test_campaign_wizard_ui.py runs the
    # wizard's node suites (one case per step against the real API's bodies, its visible text);
    # test_campaign_wizard_journey.py is the Playwright journey with its decisions pinned.
    "M177": (
        "tests/integration/test_campaign_wizard_ui.py",
        "tests/integration/test_campaign_wizard_journey.py",
        "tests/integration/test_visible_text_ui.py",
    ),
    # M178 (DEC-1608), Reports. test_reports_ui.py runs reports/tabs.test.mjs and reports/audit_equal.test.mjs and
    # checks the audit result through the new form against the old form's on the real API; test_visible_text_ui.py
    # draws the four tabs with empty allow-lists; test_ui_sweep.py opens them with a clean console and reaches
    # #/reports by clicks; test_simple_ui.py runs settings.test.mjs (Reports' way in from Settings).
    "M178": (
        "tests/integration/test_reports_ui.py",
        "tests/integration/test_visible_text_ui.py",
        "tests/integration/test_ui_sweep.py",
        "tests/integration/simple/test_simple_ui.py",
    ),
    # M179 (DEC-1609), the Data page and GET /uploads. test_data_ui.py runs the sources, actions, check and loaded
    # node suites.
    "M179": (
        "tests/integration/test_api_uploads_list.py",
        "tests/integration/data_page/test_data_ui.py",
        "tests/integration/test_visible_text_ui.py",
    ),
    # M180 (DEC-1610), "Show technical details". test_production_ui_js.py runs technical_signed_in.test.mjs;
    # test_simple_ui.py runs technical.test.mjs and reports.test.mjs; test_visible_text_ui.py checks both switch
    # positions in both layouts; test_technical_switch_browser.py is the switch in Chromium.
    "M180": (
        "tests/integration/test_api_config.py",
        "tests/integration/production/test_preferences_routes.py",
        "tests/integration/production/test_production_ui_js.py",
        "tests/integration/simple/test_simple_ui.py",
        "tests/integration/test_visible_text_ui.py",
        "tests/integration/test_technical_switch_browser.py",
        "tests/unit/decide/test_contact_list_js.py",
    ),
    # M181 (DEC-1611), usability evidence, parts A to C (the switch-over waits for the five-person test).
    # test_usability_kit_docs.py pins the kit; the accessibility, keyboard and screenshot checks are part A;
    # test_usability_journeys.py pins both tasks' clicks and asserts the target of 5 (part C); test_model_not_approved.py
    # is the server's hold on a list made with an unapproved model; test_getting_started_doc.py the guide.
    "M181": (
        "tests/unit/test_usability_kit_docs.py",
        "tests/integration/test_accessibility_ui.py",
        "tests/integration/test_keyboard_path_ui.py",
        "tests/integration/test_ui_screenshots.py",
        "tests/integration/test_usability_journeys.py",
        "tests/integration/test_campaign_wizard_journey.py",
        "tests/integration/production/test_model_not_approved.py",
        "tests/unit/test_getting_started_doc.py",
    ),
    # M182 (DEC-1612), the brand as configuration. test_brand_ui.py runs the brand node suites and the differential
    # test against main's pages; test_brand_browser.py is slow (Chromium).
    "M182": (
        "tests/integration/production/test_brand_setting.py",
        "tests/integration/test_brand_ui.py",
        "tests/integration/test_brand_browser.py",
    ),
    # M183 (DEC-1613), the usability session pack (the command, the recruitment note and the consent form).
    "M183": (
        "tests/unit/test_usability_session.py",
        "tests/unit/test_usability_kit_docs.py",
    ),
    # M184 (DEC-1614), safety fixes from the autonomy audit: an approved plan's list keeps its limits, unattended
    # steps use only approved models, the strict compliance gate, copy approval's separation of duties, identity.
    "M184": (
        "tests/agentic/test_planner_safety.py",
        "tests/unit/decide/test_campaign_scope.py",
        "tests/unit/production/test_firing_model_approval.py",
        "tests/unit/compliance/test_gate_strict.py",
        "tests/integration/test_api_generative_copy_separation.py",
        "tests/integration/test_api_generative_copy_identity.py",
        "tests/integration/production/test_approvals_identity.py",
        "tests/unit/agentic/test_verified_identity.py",
    ),
    # --- Plan O (docs/plans/MARKETING_AI_PLAN_O_DATA_DOCTOR.md); each milestone adds its own entry. ---
    # M230 (DEC-1800), the set-up: the PLAN-O blocks, PLAN_O_CODES in the catalogue check, the switch
    # `data_doctor.unified` (off, validated, round-trips, left out of run_config.json while off).
    "M230": (
        "tests/unit/test_shared_file_markers.py",
        "tests/unit/pilot/test_help.py",
        "tests/unit/doctor/test_plan_o_setup.py",
    ),
}
"""Milestone id -> the test files (or directories, with a trailing `/`) that prove it is built.

Taken from the commits that delivered each milestone. A milestone may be absent - Phase 4b's M46
... M52 are, and so is anything planned but not started - and an absent milestone is reported, never
flagged. A mapping must name tests that fail
when the milestone's definition of done is not met; tests of a part that passes on its own are not
evidence for the whole. Paths are relative to the repository root, the way pytest's node ids spell them."""

PENDING: Final[re.Pattern[str]] = re.compile(
    r"^(pending|not (yet )?(started|begun|built|done)|to ?do|tbd|planned)\b"
)
"""A status that claims the milestone is not built. Matched at the START of the normalised cell,
so "done — pending a laptop run" is a done milestone with a caveat, not a pending one, and
"partial" is neither: a partial milestone may have every test green and still owe something the
tests cannot see (M7's laptop measurement)."""

MILESTONE_ID: Final[re.Pattern[str]] = re.compile(r"^M\d+[a-z]?$")
FENCE: Final[re.Pattern[str]] = re.compile(r"^\s*(```|~~~)")
SEPARATOR_CELL: Final[re.Pattern[str]] = re.compile(r"^:?-{3,}:?$")
PAID_MARKERS: Final[str] = "not slow and not bedrock and not aws"
"""What the fallback run selects: the fast suite's filter, the same as `make test`'s."""


class CheckError(Exception):
    """The check could not reach a verdict (as opposed to reaching "dishonest")."""


@dataclass(frozen=True)
class MilestoneRow:
    """One row of a README milestone table."""

    milestone: str
    status: str
    line: int

    @property
    def pending(self) -> bool:
        return PENDING.match(self.status) is not None


@dataclass(frozen=True)
class Evidence:
    """What one test report says about one milestone's tests."""

    passed: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def proves_built(self) -> bool:
        return self.passed > 0 and self.failed == 0


def _normalise(cell: str) -> str:
    """`**done** — see below` -> `done — see below`: emphasis and code marks carry no meaning here."""
    return re.sub(r"[*_`]", "", cell).strip().lower()


def _cells(line: str) -> list[str]:
    """Split one table row. `\\|` is a literal pipe inside a cell, not a column boundary."""
    body = line.strip()
    body = body.removeprefix("|").removesuffix("|")
    return [cell.replace("\\|", "|").strip() for cell in re.split(r"(?<!\\)\|", body)]


def _tables(lines: Sequence[str]) -> Iterable[tuple[int, list[str]]]:
    """Yield `(first line number, rows)` for each Markdown table outside a fenced code block."""
    in_fence = False
    block: list[str] = []
    start = 0
    for number, line in enumerate(lines, start=1):
        if FENCE.match(line):
            in_fence = not in_fence
        is_row = not in_fence and line.lstrip().startswith("|")
        if is_row:
            if not block:
                start = number
            block.append(line)
            continue
        if block:
            yield start, block
            block = []
    if block:
        yield start, block


def milestone_rows(readme_text: str) -> list[MilestoneRow]:
    """Every milestone row of every table that has a `Status` column."""
    rows: list[MilestoneRow] = []
    for start, block in _tables(readme_text.splitlines()):
        if len(block) < 2 or not all(SEPARATOR_CELL.match(c.replace(" ", "")) for c in _cells(block[1])):
            continue
        header = [_normalise(cell) for cell in _cells(block[0])]
        if "status" not in header:
            continue
        status_at = header.index("status")
        for offset, line in enumerate(block[2:], start=2):
            cells = _cells(line)
            if len(cells) <= status_at:
                continue
            milestone = re.sub(r"[*_`]", "", cells[0]).strip()
            if MILESTONE_ID.match(milestone):
                rows.append(MilestoneRow(milestone, _normalise(cells[status_at]), start + offset))
    return rows


def _module_prefix(path: str) -> str:
    """`tests/unit/test_x.py` -> `tests.unit.test_x`; `tests/infra/` -> `tests.infra.`.

    That is how pytest's JUnit writer spells a test's `classname` (its node id with `/` turned into
    `.` and `.py` dropped), so the comparison needs no second source of truth about file layout.
    """
    if path.endswith("/"):
        return path.rstrip("/").replace("/", ".") + "."
    return path.removesuffix(".py").replace("/", ".")


def _belongs(classname: str, path: str) -> bool:
    prefix = _module_prefix(path)
    if prefix.endswith("."):
        return classname.startswith(prefix)
    return classname == prefix or classname.startswith(prefix + ".")


def read_junit(report: Path) -> list[tuple[str, str]]:
    """`(classname, outcome)` for every test case in a JUnit XML report.

    The outcome is `failed` for a `<failure>` or `<error>` child, `skipped` for `<skipped>` (which is
    also how pytest writes an xfail), and `passed` otherwise.
    """
    try:
        # Plain ElementTree rather than a hardened parser: the report is one our own pytest run
        # wrote, on the same runner, a step earlier.
        root = ET.parse(report).getroot()
    except (OSError, ET.ParseError) as exc:
        raise CheckError(f"Could not read the test report {report}: {type(exc).__name__}.") from exc
    cases: list[tuple[str, str]] = []
    for case in root.iter("testcase"):
        tags = {child.tag for child in case}
        outcome = "failed" if tags & {"failure", "error"} else "skipped" if "skipped" in tags else "passed"
        cases.append((case.get("classname", ""), outcome))
    return cases


def evidence_for(paths: Sequence[str], cases: Sequence[tuple[str, str]]) -> Evidence:
    """Count the report's outcomes for the test cases that live under `paths`."""
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    for classname, outcome in cases:
        if any(_belongs(classname, path) for path in paths):
            counts[outcome] += 1
    return Evidence(**counts)


def missing_paths(mapping: Mapping[str, Sequence[str]], repo_root: Path) -> list[str]:
    """Mapped paths that are not in the checkout: a rename the mapping has not caught up with."""
    return sorted(
        f"{milestone}: {path}"
        for milestone, paths in mapping.items()
        for path in paths
        if not (repo_root / path).exists()
    )


def run_pytest(paths: Sequence[str], repo_root: Path, report: Path) -> None:
    """Run the fast-suite selection of `paths` and leave its JUnit report at `report`.

    A red test is not an error here - it is evidence, and `evidence_for` reads it - but a run that
    wrote no report at all (a collection crash, a usage error) is.
    """
    command = [
        sys.executable,
        "-m",
        "pytest",
        *paths,
        "-m",
        PAID_MARKERS,
        "-p",
        "no:cacheprovider",
        f"--junitxml={report}",
    ]
    completed = subprocess.run(command, cwd=repo_root, check=False, capture_output=True, text=True)
    if not report.is_file():
        tail = "\n".join(completed.stdout.splitlines()[-20:])
        raise CheckError(f"pytest exited {completed.returncode} without a report:\n{tail}")


def verdict(
    rows: Sequence[MilestoneRow],
    cases: Sequence[tuple[str, str]],
    mapping: Mapping[str, Sequence[str]],
    *,
    readme: str,
) -> tuple[list[str], int]:
    """The lines to print and the exit code, as a pure function of the parsed inputs."""
    lines: list[str] = []
    wrong = 0
    for row in rows:
        if not row.pending:
            continue
        paths = mapping.get(row.milestone)
        if paths is None:
            lines.append(f"{readme}:{row.line}: {row.milestone} is pending; no tests are mapped to it (ok).")
            continue
        found = evidence_for(paths, cases)
        if found.proves_built:
            wrong += 1
            lines.append(
                f"{readme}:{row.line}: {row.milestone} is marked '{row.status}' but {found.passed} of its "
                f"tests passed and none failed ({', '.join(paths)}). Mark it done, or say what is missing."
            )
        else:
            lines.append(
                f"{readme}:{row.line}: {row.milestone} is pending; the report holds no evidence it is "
                f"built (passed {found.passed}, failed {found.failed}, skipped {found.skipped}) (ok)."
            )
    pending = sum(row.pending for row in rows)
    lines.append(
        f"{len(rows)} milestone rows, {pending} pending, {wrong} wrongly pending."
        if wrong
        else f"README honest: {len(rows)} milestone rows, {pending} pending, none with passing tests."
    )
    return lines, 1 if wrong else 0


def check(readme_path: Path, junit: Path | None, repo_root: Path) -> tuple[list[str], int]:
    """Parse, gather evidence (from `junit`, or by running pytest), and decide."""
    try:
        text = readme_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CheckError(f"Could not read {readme_path}: {type(exc).__name__}.") from exc
    rows = milestone_rows(text)
    if not rows:
        raise CheckError(
            f"{readme_path} has no milestone table (a table with a 'Status' column whose rows start "
            "with M<number>). Either the tables moved or the parser needs to follow them."
        )
    missing = missing_paths(MILESTONE_TESTS, repo_root)
    if missing:
        raise CheckError(
            "MILESTONE_TESTS names paths that do not exist:\n  " + "\n  ".join(missing) + "\n"
            "Update the mapping in scripts/check_readme.py to where those tests went."
        )
    if junit is not None:
        cases = read_junit(junit)
    else:
        targets = sorted(
            {path for row in rows if row.pending for path in MILESTONE_TESTS.get(row.milestone, ())}
        )
        cases = []
        if targets:
            with tempfile.TemporaryDirectory() as scratch:
                report = Path(scratch) / "junit.xml"
                run_pytest(targets, repo_root, report)
                cases = read_junit(report)
    try:
        shown = readme_path.resolve().relative_to(repo_root)
    except ValueError:
        shown = readme_path
    return verdict(rows, cases, MILESTONE_TESTS, readme=str(shown))


def main(argv: Sequence[str] | None = None) -> int:
    """Print the verdict; the exit code is what CI gates on."""
    parser = argparse.ArgumentParser(
        prog=COMMAND,
        description="Fail when README.md marks a milestone pending whose mapped tests pass.",
    )
    parser.add_argument("--readme", type=Path, default=REPO_ROOT / DEFAULT_README)
    parser.add_argument(
        "--junit",
        type=Path,
        default=None,
        help="a JUnit XML report of a run that already happened (CI passes the fast suite's); "
        "without it, the tests of pending milestones are run here",
    )
    args = parser.parse_args(argv)
    try:
        lines, code = check(args.readme, args.junit, REPO_ROOT)
    except CheckError as exc:
        print(f"check_readme: {exc}", file=sys.stderr)
        return 2
    stream = sys.stderr if code else sys.stdout
    for line in lines:
        print(line, file=stream)
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
