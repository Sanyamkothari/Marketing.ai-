"""Plan O M234: the readiness report's shape, its other probes, its approvals, storage and the tool
(DEC-1804). The nine gaps and the invariant have their own modules."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.agent.checks import check_plan
from engine.agent.contracts import DataRecipe, RecipeStep, RecipeStepKind, recipe_hash
from engine.config import ColumnRole, ColumnType, ProblemType, RunMode, load_all_use_cases
from engine.contracts import FeatureSchema, FeatureSchemaColumn
from engine.doctor.readiness import (
    CHECK_READINESS_TOOL,
    READINESS_RUN_FILENAME,
    RECIPE_ROLES_MISMATCH,
    CheckReadinessArgs,
    ReadinessInputs,
    ReadinessToolSpec,
    RecordedApproval,
    attach_readiness_to_run,
    check_readiness,
    readiness_key,
    register_readiness_tool,
    roles_refusal,
    store_readiness,
    summarise,
    use_case_type,
)
from engine.doctor.readiness_contracts import PROBE_CODES, READINESS_CODES, DataReadinessReport, OpenQuestion
from engine.doctor.readiness_probes import FIELD_TOO_LONG, SourceScan, scan_source
from engine.stages import ingest, prepare
from engine.storage import LocalStorage
from tests.fixtures.make_data import GenerationSpec, generate
from tests.unit.doctor.readiness_fixtures import CONFIGS, config, customers, report

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)

EXPECTED_TYPES = {
    "ai-onboarding-assistant": "generative_rag",
    "bank-term-deposit": "propensity",
    "card-default-propensity": "propensity",
    "fault-prediction": "time_based",
    "hillstrom-email": "propensity",  # the campaign-effect run switches it to uplift with its own overrides
    "insurance-cross-sell": "propensity",
    "order-fulfillment": "time_based",
    "payment-propensity": "propensity",
    "rca": "hybrid_rca",
    "retail-win-back": "propensity",
    "targeted-advertisement": "propensity",
    "telco-churn": "propensity",
    "win-back-campaign": "hybrid_copy",
}


def test_fifteen_probe_codes_in_the_design_order() -> None:
    assert len(PROBE_CODES) == 15 and frozenset(PROBE_CODES) == READINESS_CODES
    assert PROBE_CODES[0] == "X_TIME_ALL_PARSE" and PROBE_CODES[-1] == "X_COMPLAINT_COLUMN_PRESENT"


@pytest.mark.parametrize("use_case_id", sorted(EXPECTED_TYPES))
def test_every_shipped_use_case_gets_a_report_and_clean_data_is_ready(use_case_id: str) -> None:
    cfg = load_all_use_cases(CONFIGS)[use_case_id]
    assert use_case_type(cfg) == EXPECTED_TYPES[use_case_id]
    frame = generate(GenerationSpec(use_case_id=use_case_id, rows=2000, variant="clean", seed=1))
    key = next(column.name for column in cfg.template.columns if column.role is ColumnRole.PRIMARY_KEY)
    result = report(frame, cfg, primary_key=key, target=cfg.target.column, now=NOW)
    assert tuple(probe.code for probe in result.probes) == PROBE_CODES
    assert result.verdict in ("ready", "ready_with_warnings"), result.verdict_reason
    assert result.dry_run_passed


@pytest.mark.parametrize(
    ("levels", "values", "status"),
    [
        pytest.param((), ["1", "0"], "pass", marks=pytest.mark.slow),  # the uplift checks run in full
        ((), ["yes", "no"], "fail"),
        pytest.param(
            ("control", "offer_a", "offer_b"),
            ["control", "offer_a", "offer_b"],
            "pass",
            marks=pytest.mark.slow,
        ),
        (("control", "offer_a", "offer_b"), ["control", "offer_a", "offer_c"], "fail"),
    ],
)
def test_uplift_goals_check_the_treatment_tokens(
    levels: tuple[str, ...], values: list[str], status: str
) -> None:
    cfg = config(
        "targeted-advertisement",
        top={"problem_type": ProblemType.UPLIFT},
        uplift={"treatment_column": "treated", "treatment_levels": levels},
    )
    frame = customers(2400)
    frame["treated"] = [values[i % len(values)] for i in range(len(frame))]
    result = report(frame, cfg, now=NOW)
    assert result.use_case_type == ("uplift_multi_arm" if levels else "uplift")
    assert result.probe("X_TREATMENT_TOKENS").status == status
    assert result.uplift_report is not None
    assert any(r.gate == "uplift_validate" for r in result.requirements)
    assert result.probe("X_SPLIT_CLASSES").status == "skipped"  # the uplift flow draws its own parts
    if status == "fail":
        assert result.verdict == "not_ready"


def test_missing_channel_consent_columns_fail() -> None:
    cfg = config("targeted-advertisement")
    channels = {"sms": {"consent_column": "sms_ok"}, "email": {"consent_column": "email_ok"}}
    suppression = type(cfg.actions.suppression).model_validate(
        {**cfg.actions.suppression.model_dump(), "channels": channels}
    )
    cfg = cfg.model_copy(update={"actions": cfg.actions.model_copy(update={"suppression": suppression})})
    frame = customers()
    frame["sms_ok"] = True
    result = report(frame, cfg, now=NOW)
    probe = result.probe("X_CHANNEL_COLUMNS_PRESENT")
    assert probe.status == "fail" and probe.column == "email_ok"
    assert result.roles.channel_columns == ("sms_ok", "email_ok")


def test_a_value_column_more_than_a_tenth_empty_fails() -> None:
    cfg = config("targeted-advertisement")
    policy = cfg.uplift.policy.model_copy(update={"value_column": "order_value"})
    cfg = cfg.model_copy(update={"uplift": cfg.uplift.model_copy(update={"policy": policy})})
    frame = customers()
    frame["order_value"] = np.where(np.arange(len(frame)) % 5 == 0, np.nan, 100.0)
    assert report(frame, cfg, now=NOW).probe("X_VALUE_COLUMN_MISSING_SHARE").status == "fail"
    frame["order_value"] = 100.0
    assert report(frame, cfg, now=NOW).probe("X_VALUE_COLUMN_MISSING_SHARE").status == "pass"


def test_message_fields_must_be_filled_for_a_copy_goal() -> None:
    cfg = load_all_use_cases(CONFIGS)["win-back-campaign"]
    frame = generate(GenerationSpec(use_case_id="win-back-campaign", rows=2000, variant="clean", seed=1))
    assert (
        report(frame, cfg, primary_key="customer_id", target=cfg.target.column, now=NOW)
        .probe("X_ALLOWED_FIELDS_FILLED")
        .skip_reason
        == "none of the message fields is a column of this file"
    )
    frame["first_name"] = "Asha"
    frame.loc[:9, "first_name"] = None
    result = report(frame, cfg, primary_key="customer_id", target=cfg.target.column, now=NOW)
    probe = result.probe("X_ALLOWED_FIELDS_FILLED")
    assert probe.status == "fail" and probe.column == "first_name" and probe.measured == 10.0


def test_a_root_cause_goal_needs_its_complaint_column() -> None:
    cfg = load_all_use_cases(CONFIGS)["rca"]
    frame = generate(GenerationSpec(use_case_id="rca", rows=2000, variant="clean", seed=1))
    key = next(column.name for column in cfg.template.columns if column.role is ColumnRole.PRIMARY_KEY)
    result = report(
        frame.drop(columns=["complaint_text"]), cfg, primary_key=key, target=cfg.target.column, now=NOW
    )
    assert result.probe("X_COMPLAINT_COLUMN_PRESENT").status == "fail"
    assert result.verdict == "not_ready"


def test_the_documents_assistant_checks_its_reference_set() -> None:
    cfg = load_all_use_cases(CONFIGS)["ai-onboarding-assistant"]
    frame = generate(GenerationSpec(use_case_id="ai-onboarding-assistant", rows=60, variant="clean", seed=1))
    good = report(frame, cfg, primary_key="question", target=None, now=NOW)
    assert good.dry_run_report is None and good.dry_run_passed
    bad = report(frame.drop(columns=["source_doc"]), cfg, primary_key="question", target=None, now=NOW)
    assert bad.verdict == "not_ready"
    assert [r.engine_code for r in bad.requirements if r.status == "fail"] == ["REFERENCE_SET_INVALID"]


# -- scoring --------------------------------------------------------------------------------------
def _schema() -> FeatureSchema:
    return FeatureSchema(
        use_case_id="targeted-advertisement",
        model_version_id="v1",
        primary_key="customer_id",
        target="converted_30d",
        problem_type=ProblemType.BINARY_CLASSIFICATION,
        columns=(
            FeatureSchemaColumn(name="visits_last_7d", inferred_type=ColumnType.INTEGER),
            FeatureSchemaColumn(name="tenure_months", inferred_type=ColumnType.INTEGER),
            FeatureSchemaColumn(name="plan_tier", inferred_type=ColumnType.STRING),
        ),
        row_count_at_fit=2000,
        created_at=NOW,
    )


def test_a_scoring_report_needs_the_model_schema_and_compares_columns() -> None:
    cfg = config("targeted-advertisement")
    frame = customers().drop(columns=["converted_30d", "tenure_months"])
    with pytest.raises(ValueError, match="schema"):
        report(frame, cfg, mode=RunMode.SCORE, target=None)
    result = report(frame, cfg, mode=RunMode.SCORE, target=None, schema=_schema(), now=NOW)
    assert result.mode == "score" and result.verdict == "not_ready"
    assert result.scoring_contract is not None
    assert result.scoring_contract.missing == ("tenure_months",)
    assert "ad_ctr_90d" in result.scoring_contract.extra
    assert any(r.id == "SCHEMA_MISMATCH" and r.status == "fail" for r in result.requirements)
    assert result.probe("X_SPLIT_CLASSES").skip_reason == "a scoring file is not split or trained on"


# -- answers and approvals -----------------------------------------------------------------------
def test_an_open_blocking_question_means_needs_answers() -> None:
    question = OpenQuestion(id="q1", blocking=True, text="Which column says who bought?")
    result = report(customers(), config("targeted-advertisement"), open_questions=(question,), now=NOW)
    assert result.verdict == "needs_answers"
    answered = question.model_copy(update={"answered": True})
    assert (
        report(customers(), config("targeted-advertisement"), open_questions=(answered,), now=NOW).verdict
        == "ready"
    )


def test_a_lowered_limit_waits_for_a_recorded_yes_with_the_user_id() -> None:
    cfg = config("targeted-advertisement", validation={"min_rows": 500})
    overrides = {"validation.min_rows": 500}
    waiting = report(customers(800), cfg, resolved_overrides=overrides, now=NOW)
    assert waiting.verdict == "needs_approval"
    assert [(a.kind, a.key, a.recorded) for a in waiting.approvals_required] == [
        ("threshold_change", "validation.min_rows", False)
    ]
    approved = report(
        customers(800),
        cfg,
        resolved_overrides=overrides,
        approvals=(RecordedApproval(key="validation.min_rows", approved_by="user-7", approved_at=NOW),),
        now=NOW,
    )
    assert approved.verdict == "ready"
    assert approved.approvals_required[0].approved_by == "user-7"
    assert approved.settings_applied[0].path == "validation.min_rows"


def test_an_acknowledged_leak_needs_approval() -> None:
    frame = customers()
    frame["converted_flag_copy"] = frame["converted_30d"]  # a perfect copy of the outcome: a leak
    cfg = config(
        "targeted-advertisement", validation={"acknowledged": ("LEAKAGE_SUSPECTED:converted_flag_copy",)}
    )
    result = report(frame, cfg, now=NOW)
    assert result.dry_run_passed, result.verdict_reason
    assert result.verdict == "needs_approval"
    assert result.approvals_required[0].kind == "ack_error"
    assert result.acknowledgements[0].approved_by is None
    approved = report(
        frame,
        cfg,
        approvals=(RecordedApproval(key="LEAKAGE_SUSPECTED:converted_flag_copy", approved_by="user-3"),),
        now=NOW,
    )
    assert approved.verdict in ("ready", "ready_with_warnings")
    assert approved.acknowledgements[0].approved_by == "user-3"


def test_without_the_stored_file_the_report_says_what_it_did_not_measure() -> None:
    result = check_readiness(
        ReadinessInputs(
            frame=customers(),
            config=config("targeted-advertisement"),
            mode=RunMode.TRAIN,
            primary_key="customer_id",
            target="converted_30d",
            upload_id="u1",
            row_count=2000,
            now=NOW,
        )
    )
    assert result.probe("X_PARSER_ROW_LOSS").status == "warn"
    assert result.verdict == "ready_with_warnings"
    sampled = report(customers(), config("targeted-advertisement"), row_count=5000, now=NOW)
    assert sampled.probe("X_PK_UNIQUE_FULL").status == "warn"
    # The run prepares the rows it reads (up to the row cap), so the counts are of those rows, never scaled.
    assert sampled.post_filter_counts is not None and not sampled.post_filter_counts.estimated
    assert sampled.post_filter_counts.rows_after_filters == 2000


# -- no cell value, a stable hash, storage -------------------------------------------------------
def test_the_report_carries_no_cell_value_and_a_stable_hash() -> None:
    frame = customers()
    frame["region"] = "SECRET-REGION-VALUE"
    frame.loc[0, "customer_id"] = "SECRET-ID-VALUE"
    cfg = config("targeted-advertisement", governance={"consent_column": "consent"})
    frame["consent"] = "SECRET-CONSENT-WORD"
    first = report(frame, cfg, now=NOW)
    text = first.model_dump_json(exclude={"dry_run_report", "uplift_report"})
    assert "SECRET" not in text
    assert "SECRET" not in summarise(first).model_dump_json()
    later = report(frame, cfg, now=datetime(2026, 10, 13, tzinfo=UTC))
    assert later.report_hash == first.report_hash and len(first.report_hash) == 64


def test_stored_in_the_case_and_beside_the_run_recipe(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    result = report(customers(), config("targeted-advertisement"), goal="churn_q4", now=NOW)
    key = store_readiness(storage, result, client_id="acme", case_id="case-1")
    assert key == f"cases/acme/case-1/readiness/churn_q4_{result.report_hash[:16]}.json"
    assert storage.read_model(key, DataReadinessReport) == result
    run_key = attach_readiness_to_run(storage, result, "run-1")
    assert run_key.endswith(f"run-1/{READINESS_RUN_FILENAME}")
    with pytest.raises(ValueError):
        readiness_key(client_id="../other", case_id="c", goal=None, report_hash=result.report_hash)


# -- the tool -------------------------------------------------------------------------------------
class _Context:
    def __init__(self, inputs: ReadinessInputs) -> None:
        self.inputs = inputs
        self.asked: list[CheckReadinessArgs] = []

    def readiness_inputs(self, args: CheckReadinessArgs) -> ReadinessInputs:
        self.asked.append(args)
        return self.inputs


def test_check_readiness_registers_as_an_aggregate_read_tool() -> None:
    registered: list[ReadinessToolSpec] = []
    spec = register_readiness_tool(registered.append)
    assert registered == [spec]
    assert (spec.name, spec.kind, spec.exposure) == (CHECK_READINESS_TOOL, "read", "aggregate")
    assert spec.input_model.model_config.get("extra") == "forbid"
    with pytest.raises(ValueError):
        CheckReadinessArgs.model_validate({"goal": None, "sql": "drop table"})
    inputs: dict[str, Any] = {
        "frame": customers(),
        "config": config("targeted-advertisement"),
        "mode": RunMode.TRAIN,
        "primary_key": "customer_id",
        "target": "converted_30d",
        "upload_id": "u1",
        "row_count": 2000,
        "scan": SourceScan(file_format="parquet", rows_parsed=2000),
        "now": NOW,
    }
    context = _Context(ReadinessInputs(**inputs))
    summary = spec.run(CheckReadinessArgs(goal="churn_q4"), context)
    assert summary.verdict == "ready" and len(summary.probes) == 15
    assert context.asked[0].goal == "churn_q4"


def test_a_frame_from_a_csv_reads_the_same_as_the_frame(tmp_path: Path) -> None:
    frame = customers()
    path = tmp_path / "x.csv"
    frame.to_csv(path, index=False)
    again = pd.read_csv(path)
    assert report(again, config("targeted-advertisement"), now=NOW).verdict == "ready"


# -- review fixes (M234): counts past the row cap, skipped checks, a prepare that stops, the CSV limit ---
def test_post_filter_counts_are_of_the_rows_the_run_reads_never_scaled_past_the_cap() -> None:
    """The reviewer's reproduction: 3,000 rows read with a 2,500-row cap; consent removes 60 positives.

    The run prepares the 2,500 rows it reads and gets 190 positives (200 needed). Scaling them to the
    file's 3,000 rows said 228 and passed; the probe must say what training gets.
    """
    cfg = config("targeted-advertisement", governance={"consent_column": "marketing_opt_in"})
    frame = customers(3000, positive_rate=0.0)
    positive = np.zeros(len(frame), dtype=bool)
    positive[:2500:10] = True  # 250 positives among the rows the run reads
    positive[2500::5] = True  # 100 more past the cap, which the run never reads
    frame["converted_30d"] = positive.astype(int)
    frame["marketing_opt_in"] = True
    frame.loc[np.flatnonzero(positive[:2500])[:60], "marketing_opt_in"] = False
    read = frame.head(2500)
    assert dry_run_passes(read, cfg, row_count=3000)  # TARGET_TOO_FEW_POSITIVES counts 250 on the read rows
    rows, _plan = prepare.prepare_rows(read, cfg, primary_key=("customer_id",), target="converted_30d")
    assert int(rows["converted_30d"].sum()) == 190  # what training gets
    result = report(read, cfg, row_count=3000, now=NOW)
    assert result.verdict == "not_ready", result.verdict_reason
    probe = result.probe("X_POST_FILTER_THRESHOLDS")
    assert probe.status == "fail" and "X_POST_FILTER_THRESHOLDS" in result.verdict_reason
    assert probe.details["positives_after_filters"] == 190
    assert probe.details["rows_in"] == 2500 and probe.details["rows_in_file"] == 3000
    counts = result.post_filter_counts
    assert counts is not None and not counts.estimated and counts.positives_after_filters == 190


def dry_run_passes(frame: pd.DataFrame, cfg: Any, *, row_count: int) -> bool:
    return check_plan(
        frame,
        cfg,
        mode=RunMode.TRAIN,
        primary_key="customer_id",
        target="converted_30d",
        upload_id="u-fixture",
        row_count=row_count,
    ).passed


def test_a_check_that_did_not_run_is_listed_as_skipped_with_its_reason() -> None:
    result = report(customers(), config("targeted-advertisement"), now=NOW)  # no consent column, random split
    by_id = {r.id: r for r in result.requirements}
    assert by_id["CONSENT_COLUMN_MISSING"].status == "skipped"
    assert by_id["CONSENT_COLUMN_MISSING"].skip_reason == "no consent column configured"
    assert by_id["TIME_COLUMN_MISSING"].status == "skipped"
    assert by_id["TIME_COLUMN_MISSING"].skip_reason == "split is not time-based"
    assert by_id["PK_NOT_UNIQUE"].status == "pass" and by_id["PK_NOT_UNIQUE"].skip_reason is None
    amounts = customers()
    amounts["converted_30d"] = np.random.default_rng(3).random(len(amounts)).round(2) * 100  # an amount
    regression = report(
        amounts, config("targeted-advertisement", top={"problem_type": ProblemType.REGRESSION}), now=NOW
    )
    assert {r.id: r.status for r in regression.requirements}["TARGET_TOO_FEW_POSITIVES"] == "skipped"
    assert result.verdict == "ready"  # a skip is not a failure


@pytest.mark.parametrize("mode", [RunMode.TRAIN, RunMode.SCORE])
def test_the_dry_run_report_is_check_plans_own(mode: RunMode) -> None:
    cfg = config("targeted-advertisement")
    frame = customers(1500, seed=4)
    frame.loc[:9, "customer_id"] = frame.loc[10:19, "customer_id"].to_numpy()  # a finding in both modes
    if mode is RunMode.SCORE:
        frame = frame.drop(columns=["converted_30d", "tenure_months"])
    target = "converted_30d" if mode is RunMode.TRAIN else None
    schema = _schema() if mode is RunMode.SCORE else None
    theirs = check_plan(
        frame,
        cfg,
        mode=mode,
        primary_key="customer_id",
        target=target,
        upload_id="u-fixture",
        row_count=len(frame),
        schema=schema,
    )
    ours = report(frame, cfg, mode=mode, target=target, schema=schema, now=NOW).dry_run_report
    assert ours is not None
    assert ours.model_dump(exclude={"validated_at"}) == theirs.model_dump(exclude={"validated_at"})


def test_a_prepare_step_that_stops_fails_the_report(monkeypatch: pytest.MonkeyPatch) -> None:
    def stops(*_args: Any, **_kwargs: Any) -> Any:
        raise TypeError("unhashable type: 'list'")

    monkeypatch.setattr(prepare, "prepare_rows", stops)
    result = report(customers(), config("targeted-advertisement"), now=NOW)
    probe = result.probe("X_POST_FILTER_THRESHOLDS")
    assert probe.status == "fail" and probe.details["error"] == "TypeError"
    assert "unhashable" not in probe.message  # the class only: a message can quote a value
    assert result.verdict == "not_ready"
    assert result.probe("X_TIME_ALL_PARSE").status == "skipped"


def test_counting_the_csv_leaves_the_process_wide_field_limit_alone(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    frame = customers(50)
    frame["notes"] = "short"
    frame.loc[3, "notes"] = "x" * (csv.field_size_limit() + 10)  # longer than the default limit
    storage.write_bytes("uploads/u1/source.csv", frame.to_csv(index=False).encode())
    before = csv.field_size_limit()
    scan = scan_source(
        storage,
        "uploads/u1/source.csv",
        file_format="csv",
        rows_parsed=50,
        key_columns=("customer_id",),
    )
    assert csv.field_size_limit() == before
    assert scan.parse_error == FIELD_TOO_LONG
    result = report(frame, config("targeted-advertisement"), scan=scan, now=NOW)
    probe = result.probe("X_PARSER_ROW_LOSS")
    assert probe.status == "warn" and probe.details["error"] == FIELD_TOO_LONG
    plain = customers(50)
    storage.write_bytes("uploads/u2/source.csv", plain.to_csv(index=False).encode())
    clean = scan_source(storage, "uploads/u2/source.csv", file_format="csv", rows_parsed=50)
    assert clean.parse_error is None and clean.rows_physical == 50
    assert csv.field_size_limit() == before


def _recipe(primary_key: str | None, target: str | None) -> DataRecipe:
    steps = (RecipeStep(order=1, kind=RecipeStepKind.DROP_COLUMN, column="region"),)
    return DataRecipe(
        recipe_id="rec1",
        use_case_id="targeted-advertisement",
        source_fingerprint="f",
        steps=steps,
        recipe_hash=recipe_hash(steps),
        primary_key=primary_key,
        target=target,
        created_at=NOW,
    )


def test_roles_refusal_is_refuse_other_roles_rule() -> None:
    recipe = _recipe("customer_id", "converted_30d")
    same: dict[str, Any] = {
        "primary_key": "customer_id",
        "target": "converted_30d",
        "applies_to": "propensity:train",
    }
    assert roles_refusal(recipe, training=True, **same) is None
    assert roles_refusal(None, training=True, **{**same, "target": "other"}) is None
    other_target = roles_refusal(recipe, training=True, **{**same, "target": "marketing_opt_in"})
    assert other_target is not None and other_target.id == RECIPE_ROLES_MISMATCH
    assert other_target.gate == "recipe_replay" and other_target.status == "fail" and other_target.blocking
    assert roles_refusal(recipe, training=True, **{**same, "primary_key": ["customer_id", "region"]})
    # scoring compares the ID column only, and only when the recipe named one
    assert roles_refusal(recipe, training=False, **{**same, "target": None}) is None
    assert roles_refusal(recipe, training=False, **{**same, "primary_key": "region", "target": None})
    assert roles_refusal(_recipe(None, None), training=False, **{**same, "primary_key": "region"}) is None


def test_a_refusal_before_validation_fails_the_dry_run() -> None:
    refusal = roles_refusal(
        _recipe("customer_id", "converted_30d"),
        primary_key="customer_id",
        target="marketing_opt_in",
        training=True,
        applies_to="propensity:train",
    )
    assert refusal is not None
    result = report(customers(), config("targeted-advertisement"), refusals=(refusal,), now=NOW)
    assert not result.dry_run_passed and result.dry_run_report is None
    assert result.verdict == "not_ready" and RECIPE_ROLES_MISMATCH in result.verdict_reason
    assert summarise(result).failing_requirements == (RECIPE_ROLES_MISMATCH,)


def test_a_scoring_report_from_storage_needs_the_model_version(tmp_path: Path) -> None:
    from engine.doctor.readiness import inputs_for_upload

    with pytest.raises(ValueError, match="model version"):
        inputs_for_upload(
            LocalStorage(tmp_path),
            "u1",
            config=config("targeted-advertisement"),
            mode=RunMode.SCORE,
            primary_key="customer_id",
            target=None,
            schema=_schema(),
        )
    assert ingest.profile_row_cap(config("targeted-advertisement")) > 0
